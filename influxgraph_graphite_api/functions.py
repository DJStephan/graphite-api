# coding: utf-8
# Copyright 2008 Orbitz WorldWide
# Copyright 2014 Bruno Renié
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from operator import is_not, itemgetter

import six
from six.moves import map, reduce, zip_longest

from .render.attime import parseATTime, parseTimeOffset
from .render.datalib import fetchData, TimeSeries
from .render.grammar import grammar
from .utils import epoch, to_seconds
try:
    from .render.glyph import format_units
except NameError:
    CAIRO_DISABLED = True
else:
    CAIRO_DISABLED = False

NAN = float('NaN')
INF = float('inf')
MINUTE = 60
HOUR = MINUTE * 60
DAY = HOUR * 24


# Utility functions
not_none = partial(filter, partial(is_not, None))


def not_empty(values):
    for v in values:
        if v is not None:
            return True
    return False


def safe(f):
    def inner(values):
        vals = list(not_none(values))
        if not vals:
            return
        return f(vals)
    return inner


safeSum = safe(sum)
safeMin = safe(min)
safeMax = safe(max)


@safe
def safeDiff(safeValues):
    values = list(map(lambda x: -x, safeValues[1:]))
    values.insert(0, safeValues[0])
    return sum(values)


def safeLen(values):
    return len(list(not_none(values)))


def safeDiv(a, b):
    if a is None:
        return None
    if b in (0, None):
        return None
    return float(a) / float(b)


def safePow(a, b):
    if a is None:
        return None
    if b is None:
        return None
    try:
        result = math.pow(a, b)
    except ValueError:
        return None
    except OverflowError:
        return None
    return result


def safeMul(*factors):
    if None in factors:
        return
    product = 1
    for factor in factors:
        product *= float(factor)
    return product


def safeSubtract(a, b):
    if a is None or b is None:
        return None
    return float(a) - float(b)


def safeAvg(a):
    return safeDiv(safeSum(a), safeLen(a))


def safeStdDev(a):
    sm = safeSum(a)
    ln = safeLen(a)
    avg = safeDiv(sm, ln)
    if avg is None:
        return None
    sum = 0
    for val in not_none(a):
        sum = sum + (val - avg) * (val - avg)
    return math.sqrt(sum/ln)


def safeLast(values):
    for v in reversed(values):
        if v is not None:
            return v


def safeMap(function, values):
    safeValues = list(not_none(values))
    if safeValues:
        return [function(x) for x in safeValues]


def safeAbs(value):
    if value is None:
        return None
    return abs(value)


# Greatest common divisor
def gcd(a, b):
    if b == 0:
        return a
    return gcd(b, a % b)


# Least common multiple
def lcm(a, b):
    if a == b:
        return a
    if a < b:
        a, b = b, a  # ensure a > b
    return a / gcd(a, b) * b


def normalize(seriesLists):
    if not seriesLists:
        return []
    seriesList = reduce(lambda L1, L2: L1+L2, seriesLists)
    step = reduce(lcm, [s.step for s in seriesList])
    for s in seriesList:
        s.consolidate(step // s.step)
    start = min([s.start for s in seriesList])
    end = max([s.end for s in seriesList])
    end -= (end - start) % step
    return seriesList, start, end, step


def matchSeries(seriesList1, seriesList2):
    assert len(seriesList2) == len(seriesList1), ("The number of series in "
                                                  "each argument must be the "
                                                  "same")
    return zip_longest(sorted(seriesList1, key=lambda x: x.name),
                       sorted(seriesList2, key=lambda x: x.name))


def formatPathExpressions(seriesList):
    """
    Returns a comma-separated list of unique path expressions.
    """
    pathExpressions = sorted(set([s.pathExpression for s in seriesList]))
    return ','.join(pathExpressions)

# Series Functions


def sumSeries(requestContext, *seriesLists):
    """
    Short form: sum()

    This will add metrics together and return the sum at each datapoint. (See
    integral for a sum over time)

    Example::

        &target=sum(company.server.application*.requestsHandled)

    This would show the sum of all requests handled per minute (provided
    requestsHandled are collected once a minute).     If metrics with different
    retention rates are combined, the coarsest metric is graphed, and the sum
    of the other metrics is averaged for the metrics with finer retention
    rates.

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "sumSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeSum(row) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def sumSeriesWithWildcards(requestContext, seriesList, *positions):
    """
    Call sumSeries after inserting wildcards at the given position(s).

    Example::

        &target=sumSeriesWithWildcards(host.cpu-[0-7].cpu-{user,system}.value,
                                       1)

    This would be the equivalent of::

        &target=sumSeries(host.*.cpu-user.value)&target=sumSeries(
            host.*.cpu-system.value)

    """
    newSeries = {}
    newNames = list()

    for series in seriesList:
        newname = '.'.join(map(lambda x: x[1],
                               filter(lambda i: i[0] not in positions,
                                      enumerate(series.name.split('.')))))
        if newname in newSeries:
            newSeries[newname] = sumSeries(requestContext,
                                           (series, newSeries[newname]))[0]
        else:
            newSeries[newname] = series
            newNames.append(newname)
        newSeries[newname].name = newname

    return [newSeries[name] for name in newNames]


def averageSeriesWithWildcards(requestContext, seriesList, *positions):
    """
    Call averageSeries after inserting wildcards at the given position(s).

    Example::

        &target=averageSeriesWithWildcards(
            host.cpu-[0-7].cpu-{user,system}.value, 1)

    This would be the equivalent of::

        &target=averageSeries(host.*.cpu-user.value)&target=averageSeries(
            host.*.cpu-system.value)

    """
    matchedList = defaultdict(list)
    for series in seriesList:
        newname = '.'.join(map(lambda x: x[1],
                               filter(lambda i: i[0] not in positions,
                                      enumerate(series.name.split('.')))))
        matchedList[newname].append(series)
    result = []
    for name in matchedList:
        [series] = averageSeries(requestContext, (matchedList[name]))
        series.name = name
        result.append(series)
    return result


def multiplySeriesWithWildcards(requestContext, seriesList, *position):
    """
    Call multiplySeries after inserting wildcards at the given position(s).

    Example::

        &target=multiplySeriesWithWildcards(
            web.host-[0-7].{avg-response,total-request}.value, 2)

    This would be the equivalent of::

        &target=multiplySeries(web.host-0.{avg-response,total-request}.value)
        &target=multiplySeries(web.host-1.{avg-response,total-request}.value)
        ...
    """
    positions = [position] if isinstance(position, int) else position

    newSeries = {}
    newNames = []

    for series in seriesList:
        new_name = ".".join(map(lambda x: x[1],
                                filter(lambda i: i[0] not in positions,
                                       enumerate(series.name.split('.')))))

        if new_name in newSeries:
            [newSeries[new_name]] = multiplySeries(requestContext,
                                                   (newSeries[new_name],
                                                    series))
        else:
            newSeries[new_name] = series
            newNames.append(new_name)
        newSeries[new_name].name = new_name
    return [newSeries[name] for name in newNames]


def diffSeries(requestContext, *seriesLists):
    """
    Subtracts series 2 through n from series 1.

    Example::

        &target=diffSeries(service.connections.total,
                           service.connections.failed)

    To diff a series and a constant, one should use offset instead of
    (or in addition to) diffSeries.

    Example::

        &target=offset(service.connections.total, -5)

        &target=offset(diffSeries(service.connections.total,
                                  service.connections.failed), -4)
    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "diffSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeDiff(row) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def averageSeries(requestContext, *seriesLists):
    """
    Short Alias: avg()

    Takes one metric or a wildcard seriesList.
    Draws the average value of all metrics passed at each time.

    Example::

        &target=averageSeries(company.server.*.threads.busy)

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "averageSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeDiv(safeSum(row), safeLen(row))
              for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def stddevSeries(requestContext, *seriesLists):
    """

    Takes one metric or a wildcard seriesList.
    Draws the standard deviation of all metrics passed at each time.

    Example::

        &target=stddevSeries(company.server.*.threads.busy)

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "stddevSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeStdDev(row) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def minSeries(requestContext, *seriesLists):
    """
    Takes one metric or a wildcard seriesList.
    For each datapoint from each metric passed in, pick the minimum value and
    graph it.

    Example::

        &target=minSeries(Server*.connections.total)
    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "minSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeMin(row) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def maxSeries(requestContext, *seriesLists):
    """
    Takes one metric or a wildcard seriesList. For each datapoint from each
    metric passed in, pick the maximum value and graph it.

    Example::

        &target=maxSeries(Server*.connections.total)

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "maxSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeMax(row) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def rangeOfSeries(requestContext, *seriesLists):
    """
    Takes a wildcard seriesList.
    Distills down a set of inputs into the range of the series

    Example::

        &target=rangeOfSeries(Server*.connections.total)

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "rangeOfSeries(%s)" % formatPathExpressions(seriesList)
    values = (safeSubtract(max(row),
                           min(row)) for row in zip_longest(*seriesList))
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def percentileOfSeries(requestContext, seriesList, n, interpolate=False):
    """
    percentileOfSeries returns a single series which is composed of the
    n-percentile values taken across a wildcard series at each point.
    Unless `interpolate` is set to True, percentile values are actual values
    contained in one of the supplied series.
    """
    if n <= 0:
        raise ValueError(
            'The requested percent is required to be greater than 0')

    if not seriesList:
        return []
    name = 'percentileOfSeries(%s,%g)' % (seriesList[0].pathExpression, n)
    start, end, step = normalize([seriesList])[1:]
    values = [_getPercentile(row, n, interpolate)
              for row in zip_longest(*seriesList)]
    resultSeries = TimeSeries(name, start, end, step, values)
    resultSeries.pathExpression = name
    return [resultSeries]


def keepLastValue(requestContext, seriesList, limit=INF):
    """
    Takes one metric or a wildcard seriesList, and optionally a limit to the
    number of 'None' values to skip over. Continues the line with the last
    received value when gaps ('None' values) appear in your data, rather than
    breaking your line.

    Example::

        &target=keepLastValue(Server01.connections.handled)
        &target=keepLastValue(Server01.connections.handled, 10)

    """
    for series in seriesList:
        series.name = "keepLastValue(%s)" % (series.name)
        series.pathExpression = series.name
        consecutiveNones = 0
        for i, value in enumerate(series):
            series[i] = value

            # No 'keeping' can be done on the first value because we have no
            # idea what came before it.
            if i == 0:
                continue

            if value is None:
                consecutiveNones += 1
            else:
                if 0 < consecutiveNones <= limit:
                    # If a non-None value is seen before the limit of Nones is
                    # hit, backfill all the missing datapoints with the last
                    # known value.
                    for index in range(i - consecutiveNones, i):
                        series[index] = series[i - consecutiveNones - 1]

                consecutiveNones = 0

        # If the series ends with some None values, try to backfill a bit to
        # cover it.
        if 0 < consecutiveNones <= limit:
            for index in range(len(series) - consecutiveNones, len(series)):
                series[index] = series[len(series) - consecutiveNones - 1]

    return seriesList


def interpolate(requestContext, seriesList, limit=INF):
    """
    Takes one metric or a wildcard seriesList, and optionally a limit to the
    number of 'None' values to skip over. Continues the line with the last
    received value when gaps ('None' values) appear in your data, rather than
    breaking your line.

    Example::

        &target=interpolate(Server01.connections.handled)
        &target=interpolate(Server01.connections.handled, 10)
    """
    for series in seriesList:
        series.name = "interpolate(%s)" % (series.name)
        series.pathExpression = series.name
        consecutiveNones = 0
        for i, value in enumerate(series):
            series[i] = value

            # No 'keeping' can be done on the first value because we have no
            # idea what came before it.
            if i == 0:
                continue

            if value is None:
                consecutiveNones += 1
            elif consecutiveNones == 0:
                # Have a value but no need to interpolate
                continue
            elif series[i - consecutiveNones - 1] is None:
                # Have a value but can't interpolate: reset count
                consecutiveNones = 0
                continue
            else:
                # Have a value and can interpolate. If a non-None value is
                # seen before the limit of Nones is hit, backfill all the
                # missing datapoints with the last known value.
                if consecutiveNones > 0 and consecutiveNones <= limit:
                    lastIndex = i - consecutiveNones - 1
                    lastValue = series[lastIndex]
                    for index in range(i - consecutiveNones, i):
                        nextValue = lastValue + (index - lastIndex)
                        nextValue = nextValue * (value - lastValue)
                        nextValue = nextValue / (consecutiveNones + 1)
                        series[index] = nextValue

                consecutiveNones = 0

    return seriesList


def changed(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.
    Output 1 when the value changed, 0 when null or the same
    Example::

        &target=changed(Server01.connections.handled)
    """
    for series in seriesList:
        series.name = series.pathExpression = 'changed(%s)' % series.name
        previous = None
        for index, value in enumerate(series):
            if previous is None:
                series[index] = 0
            elif value is not None and previous != value:
                series[index] = 1
            else:
                series[index] = 0
            previous = value
    return seriesList


def asPercent(requestContext, seriesList, total=None):
    """

    Calculates a percentage of the total of a wildcard series. If `total` is
    specified, each series will be calculated as a percentage of that total.
    If `total` is not specified, the sum of all points in the wildcard series
    will be used instead.

    The `total` parameter may be a single series, reference the same number of
    series as `seriesList` or a numeric value.

    Example::

        &target=asPercent(Server01.connections.{failed,succeeded},
                          Server01.connections.attempted)
        &target=asPercent(Server*.connections.{failed,succeeded},
                          Server*.connections.attempted)
        &target=asPercent(apache01.threads.busy,1500)
        &target=asPercent(Server01.cpu.*.jiffies)

    """
    if not seriesList:
        return []
    normalize([seriesList])

    if total is None:
        totalValues = [safeSum(row) for row in zip_longest(*seriesList)]
        totalText = "sumSeries(%s)" % formatPathExpressions(seriesList)
    elif type(total) is list:
        if len(total) != 1 and len(total) != len(seriesList):
            raise ValueError("asPercent second argument must be missing, a "
                             "single digit, reference exactly 1 series or "
                             "reference the same number of series as the "
                             "first argument")

        if len(total) == 1:
            normalize([seriesList, total])
            totalValues = total[0]
            totalText = totalValues.name
    else:
        totalValues = [total] * len(seriesList[0])
        totalText = str(total)

    resultList = []
    if type(total) is list and len(total) == len(seriesList):
        for series1, series2 in matchSeries(seriesList, total):
            name = "asPercent(%s,%s)" % (series1.name, series2.name)
            (seriesList, start, end, step) = normalize([(series1, series2)])
            resultValues = [safeMul(safeDiv(v1, v2), 100.0)
                            for v1, v2 in zip_longest(series1, series2)]
            resultSeries = TimeSeries(name, start, end, step, resultValues)
            resultSeries.pathExpression = name
            resultList.append(resultSeries)
    else:
        for series in seriesList:
            resultValues = [safeMul(safeDiv(val, totalVal), 100.0)
                            for val, totalVal in zip_longest(series,
                                                             totalValues)]
            name = "asPercent(%s,%s)" % (series.name, totalText or
                                         series.pathExpression)
            resultSeries = TimeSeries(name, series.start, series.end,
                                      series.step, resultValues)
            resultSeries.pathExpression = name
            resultList.append(resultSeries)

    return resultList


def divideSeriesLists(requestContext, dividendSeriesList, divisorSeriesList):
    """
    Iterates over a two lists and divides list1[0] by list2[0], list1[1] by
    list2[1] and so on. The lists need to be the same length
    """

    if len(dividendSeriesList) != len(divisorSeriesList):
        raise ValueError("dividendSeriesList and divisorSeriesList argument\
                         must have equal length")

    results = []

    for dividendSeries, divisorSeries in zip(dividendSeriesList,
                                             divisorSeriesList):
        name = "divideSeries(%s,%s)" % (dividendSeries.name,
                                        divisorSeries.name)
        bothSeries = (dividendSeries, divisorSeries)
        step = reduce(lcm, [s.step for s in bothSeries])

        for s in bothSeries:
            s.consolidate(step // s.step)

        start = min([s.start for s in bothSeries])
        end = max([s.end for s in bothSeries])
        end -= (end - start) % step

        values = (safeDiv(v1, v2) for v1, v2 in zip(*bothSeries))

        quotientSeries = TimeSeries(name, start, end, step, values)
        results.append(quotientSeries)

    return results


def divideSeries(requestContext, dividendSeriesList, divisorSeriesList):
    """
    Takes a dividend metric and a divisor metric and draws the division result.
    A constant may *not* be passed. To divide by a constant, use the scale()
    function (which is essentially a multiplication operation) and use the
    inverse of the dividend. (Division by 8 = multiplication by 1/8 or 0.125)

    Example::

        &target=divideSeries(Series.dividends,Series.divisors)


    """
    if len(divisorSeriesList) == 0:
        for series in dividendSeriesList:
            series.name = "divideSeries(%s,MISSING)" % series.name
            series.pathExpression = series.name
            for i in range(len(series)):
                series[i] = None
            return dividendSeriesList
    if len(divisorSeriesList) > 1:
        raise ValueError(
            "divideSeries second argument must reference exactly 1 series"
            " (got {0})".format(len(divisorSeriesList)))

    [divisorSeries] = divisorSeriesList
    results = []

    for dividendSeries in dividendSeriesList:
        name = "divideSeries(%s,%s)" % (dividendSeries.name,
                                        divisorSeries.name)
        bothSeries = (dividendSeries, divisorSeries)
        step = reduce(lcm, [s.step for s in bothSeries])

        for s in bothSeries:
            s.consolidate(step / s.step)

        start = min([s.start for s in bothSeries])
        end = max([s.end for s in bothSeries])
        end -= (end - start) % step

        values = (safeDiv(v1, v2) for v1, v2 in zip_longest(*bothSeries))

        quotientSeries = TimeSeries(name, start, end, step, values)
        quotientSeries.pathExpression = name
        results.append(quotientSeries)

    return results


def multiplySeries(requestContext, *seriesLists):
    """
    Takes two or more series and multiplies their points. A constant may not be
    used. To multiply by a constant, use the scale() function.

    Example::

        &target=multiplySeries(Series.dividends,Series.divisors)

    """

    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)

    if len(seriesList) == 1:
        return seriesList

    name = "multiplySeries(%s)" % ','.join([s.name for s in seriesList])
    product = map(lambda x: safeMul(*x), zip_longest(*seriesList))
    resultSeries = TimeSeries(name, start, end, step, product)
    resultSeries.pathExpression = name
    return [resultSeries]


def weightedAverage(requestContext, seriesListAvg, seriesListWeight, *nodes):
    """
    Takes a series of average values and a series of weights and
    produces a weighted average for all values.

    The corresponding values should share one or more zero-indexed nodes.

    Example::

        &target=weightedAverage(*.transactions.mean,*.transactions.count,0)
        &target=weightedAverage(*.transactions.mean,*.transactions.count,1,3,4)

    """

    if isinstance(nodes, int):
        nodes = [nodes]

    sortedSeries = {}

    for seriesAvg, seriesWeight in zip_longest(
            seriesListAvg, seriesListWeight):
        key = ''
        for node in nodes:
            key += seriesAvg.name.split(".")[node]

        sortedSeries.setdefault(key, {})
        sortedSeries[key]['avg'] = seriesAvg

        key = ''
        for node in nodes:
            key += seriesWeight.name.split(".")[node]

        sortedSeries.setdefault(key, {})
        sortedSeries[key]['weight'] = seriesWeight

    productList = []

    for key in sortedSeries:
        if 'weight' not in sortedSeries[key]:
            continue
        if 'avg' not in sortedSeries[key]:
            continue

        seriesWeight = sortedSeries[key]['weight']
        seriesAvg = sortedSeries[key]['avg']

        productValues = [safeMul(val1, val2)
                         for val1, val2
                         in zip_longest(seriesAvg, seriesWeight)]
        name = 'product(%s,%s)' % (seriesWeight.name, seriesAvg.name)
        productSeries = TimeSeries(name, seriesAvg.start, seriesAvg.end,
                                   seriesAvg.step, productValues)
        productSeries.pathExpression = name
        productList.append(productSeries)

    if not productList:
        return []

    [sumProducts] = sumSeries(requestContext, productList)
    [sumWeights] = sumSeries(requestContext, seriesListWeight)

    resultValues = [safeDiv(val1, val2)
                    for val1, val2 in zip_longest(sumProducts, sumWeights)]
    name = "weightedAverage(%s, %s, %s)" % (
        ','.join(sorted(set(s.pathExpression for s in seriesListAvg))),
        ','.join(sorted(set(s.pathExpression for s in seriesListWeight))),
        ','.join(map(str, nodes)))
    resultSeries = TimeSeries(name, sumProducts.start, sumProducts.end,
                              sumProducts.step, resultValues)
    resultSeries.pathExpression = name
    return resultSeries


def exponentialMovingAverage(requestContext, seriesList, windowSize):
    """
    Takes a series of values and a window size and produces an exponential
    moving average utilizing the following formula:

    ema(current) = constant * (Current Value) + (1 - constant) * ema(previous)

    The Constant is calculated as:

        constant = 2 / (windowSize + 1)

    The first period EMA uses a simple moving average for its value.

    Example::

      &target=exponentialMovingAverage(*.transactions.count, 10)
      &target=exponentialMovingAverage(*.transactions.count, '-10s')

    """
    # EMA = C * (current_value) + (1 - C) + EMA
    # C = 2 / (windowSize + 1)

    # The following was copied from movingAverage, and altered for ema
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = abs(delta.seconds + (delta.days * 86400))

    # set previewSeconds and constant based on windowSize string or integer
    if windowInterval:
        previewSeconds = windowInterval
        constant = (float(2) / (int(windowInterval) + 1))
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)
        constant = (float(2) / (int(windowSize) + 1))

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'exponentialMovingAverage(%s,"%s")' % (
                series.name, windowSize)
        else:
            newName = "exponentialMovingAverage(%s,%s)" % (
                series.name, windowSize)

        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName
        window_sum = safeSum(series[:windowPoints]) or 0
        count = safeLen(series[:windowPoints])
        ema = safeDiv(window_sum, count)
        newSeries.append(ema)

        if ema is None:
            ema = 0.0
        else:
            ema = float(ema)

        for i in range(windowPoints, len(series) - 1):
            if series[i] is not None:
                ema = (float(constant) * float(series[i]) +
                       (1 - float(constant)) * float(ema))
                newSeries.append(round(ema, 3))
            else:
                newSeries.append(None)

        result.append(newSeries)

    return result


def movingMedian(requestContext, seriesList, windowSize):
    """
    Graphs the moving median of a metric (or metrics) over a fixed number of
    past points, or a time interval.

    Takes one metric or a wildcard seriesList followed by a number N of
    datapoints or a quoted string with a length of time like '1hour' or '5min'
    (See ``from / until`` in the render\_api_ for examples of time formats).
    Graphs the median of the preceding datapoints for each point on the graph.

    Example::

        &target=movingMedian(Server.instance01.threads.busy,10)
        &target=movingMedian(Server.instance*.threads.idle,'5min')

    """
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = to_seconds(delta)

    if windowInterval:
        previewSeconds = windowInterval
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'movingMedian(%s,"%s")' % (series.name, windowSize)
        else:
            newName = "movingMedian(%s,%s)" % (series.name, windowSize)
        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName

        for i in range(windowPoints, len(series)):
            window = series[i - windowPoints:i]
            nonNull = [v for v in window if v is not None]
            if nonNull:
                m_index = len(nonNull) // 2
                newSeries.append(sorted(nonNull)[m_index])
            else:
                newSeries.append(None)
        result.append(newSeries)

    return result


def scale(requestContext, seriesList, factor):
    """
    Takes one metric or a wildcard seriesList followed by a constant, and
    multiplies the datapoint by the constant provided at each point.

    Example::

        &target=scale(Server.instance01.threads.busy,10)
        &target=scale(Server.instance*.threads.busy,10)

    """
    for series in seriesList:
        series.name = "scale(%s,%g)" % (series.name, float(factor))
        series.pathExpression = series.name
        for i, value in enumerate(series):
            series[i] = safeMul(value, factor)
    return seriesList


def scaleToSeconds(requestContext, seriesList, seconds):
    """
    Takes one metric or a wildcard seriesList and returns "value per seconds"
    where seconds is a last argument to this functions.

    Useful in conjunction with derivative or integral function if you want
    to normalize its result to a known resolution for arbitrary retentions
    """

    for series in seriesList:
        series.name = "scaleToSeconds(%s,%d)" % (series.name, seconds)
        series.pathExpression = series.name
        factor = seconds * 1.0 / series.step
        for i, value in enumerate(series):
            series[i] = safeMul(value, factor)
    return seriesList


def pow(requestContext, seriesList, factor):
    """
    Takes one metric or a wildcard seriesList followed by a constant, and
    raises the datapoint by the power of the constant provided at each point.

    Example::

        &target=pow(Server.instance01.threads.busy,10)
        &target=pow(Server.instance*.threads.busy,10)

    """
    for series in seriesList:
        series.name = "pow(%s,%g)" % (series.name, float(factor))
        series.pathExpression = series.name
        for i, value in enumerate(series):
            series[i] = safePow(value, factor)
    return seriesList


def powSeries(requestContext, *seriesLists):
    """
    Takes two or more series and pows their points. A constant line may be
    used.

    Example::

        &target=powSeries(Server.instance01.app.requests,
                          Server.instance01.app.replies)

    """
    if not seriesLists or not any(seriesLists):
        return []
    seriesList, start, end, step = normalize(seriesLists)
    name = "powSeries(%s)" % ','.join([s.name for s in seriesList])
    values = []
    for row in zip_longest(*seriesList):
        first = True
        tmpVal = None
        for element in row:
            # If it is a first iteration - tmpVal needs to be element
            if first:
                tmpVal = element
                first = False
            else:
                tmpVal = safePow(tmpVal, element)
        values.append(tmpVal)
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name
    return [series]


def squareRoot(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList, and computes the square root
    of each datapoint.

    Example::

        &target=squareRoot(Server.instance01.threads.busy)

    """
    for series in seriesList:
        series.name = "squareRoot(%s)" % (series.name)
        for i, value in enumerate(series):
            series[i] = safePow(value, 0.5)
    return seriesList


def invert(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList, and inverts each datapoint
    (i.e. 1/x).

    Example::

        &target=invert(Server.instance01.threads.busy)

    """
    for series in seriesList:
        series.name = "invert(%s)" % (series.name)
        for i, value in enumerate(series):
            series[i] = safePow(value, -1)
    return seriesList


def absolute(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList and applies the mathematical abs
    function to each datapoint transforming it to its absolute value.

    Example::

        &target=absolute(Server.instance01.threads.busy)
        &target=absolute(Server.instance*.threads.busy)
    """
    for series in seriesList:
        series.name = "absolute(%s)" % (series.name)
        series.pathExpression = series.name
        for i, value in enumerate(series):
            series[i] = safeAbs(value)
    return seriesList


def offset(requestContext, seriesList, factor):
    """
    Takes one metric or a wildcard seriesList followed by a constant, and adds
    the constant to each datapoint.

    Example::

        &target=offset(Server.instance01.threads.busy,10)

    """
    for series in seriesList:
        series.name = "offset(%s,%g)" % (series.name, float(factor))
        series.pathExpression = series.name
        for i, value in enumerate(series):
            if value is not None:
                series[i] = value + factor
    return seriesList


def offsetToZero(requestContext, seriesList):
    """
    Offsets a metric or wildcard seriesList by subtracting the minimum
    value in the series from each datapoint.

    Useful to compare different series where the values in each series
    may be higher or lower on average but you're only interested in the
    relative difference.

    An example use case is for comparing different round trip time
    results. When measuring RTT (like pinging a server), different
    devices may come back with consistently different results due to
    network latency which will be different depending on how many
    network hops between the probe and the device. To compare different
    devices in the same graph, the network latency to each has to be
    factored out of the results. This is a shortcut that takes the
    fastest response (lowest number in the series) and sets that to zero
    and then offsets all of the other datapoints in that series by that
    amount. This makes the assumption that the lowest response is the
    fastest the device can respond, of course the more datapoints that
    are in the series the more accurate this assumption is.

    Example::

        &target=offsetToZero(Server.instance01.responseTime)
        &target=offsetToZero(Server.instance*.responseTime)

    """
    for series in seriesList:
        series.name = "offsetToZero(%s)" % (series.name)
        minimum = safeMin(series)
        for i, value in enumerate(series):
            if value is not None:
                series[i] = value - minimum
    return seriesList


def movingAverage(requestContext, seriesList, windowSize):
    """
    Graphs the moving average of a metric (or metrics) over a fixed number of
    past points, or a time interval.

    Takes one metric or a wildcard seriesList followed by a number N of
    datapoints or a quoted string with a length of time like '1hour' or '5min'
    (See ``from / until`` in the render\_api_ for examples of time formats).
    Graphs the average of the preceding datapoints for each point on the graph.

    Example::

        &target=movingAverage(Server.instance01.threads.busy,10)
        &target=movingAverage(Server.instance*.threads.idle,'5min')

    """
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = to_seconds(delta)

    if windowInterval:
        previewSeconds = windowInterval
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'movingAverage(%s,"%s")' % (series.name, windowSize)
        else:
            newName = "movingAverage(%s,%s)" % (series.name, windowSize)
        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName

        windowSum = safeSum(series[:windowPoints]) or 0
        count = safeLen(series[:windowPoints])
        newSeries.append(safeDiv(windowSum, count))
        for n, last in enumerate(series[windowPoints:-1]):
            if series[n] is not None:
                windowSum -= series[n]
                count -= 1
            if last is not None:
                windowSum += last
                count += 1
            newSeries.append(safeDiv(windowSum, count))

        result.append(newSeries)

    return result


def movingSum(requestContext, seriesList, windowSize):
    """
    Graphs the moving sum of a metric (or metrics) over a fixed number of
    past points, or a time interval.

    Takes one metric or a wildcard seriesList followed by a number N of
    datapoints or a quoted string with a length of time like '1hour' or '5min'
    (See ``from / until`` in the render\_api_ for examples of time formats).
    Graphs the sum of the preceeding datapoints for each point on the graph.

    Example::

        &target=movingSum(Server.instance01.requests,10)
        &target=movingSum(Server.instance*.errors,'5min')

    """
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = abs(delta.seconds + (delta.days * 86400))

    if windowInterval:
        previewSeconds = windowInterval
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'movingSum(%s,"%s")' % (series.name, windowSize)
        else:
            newName = "movingSum(%s,%s)" % (series.name, windowSize)

        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName

        window_sum = safeSum(series[:windowPoints])
        newSeries.append(window_sum)
        for n, last in enumerate(series[windowPoints:-1]):
            if series[n] is not None:
                window_sum -= series[n]
            if last is not None:
                window_sum = (window_sum or 0) + last
            newSeries.append(window_sum)

        result.append(newSeries)

    return result


def movingMin(requestContext, seriesList, windowSize):
    """
    Graphs the moving minimum of a metric (or metrics) over a fixed number of
    past points, or a time interval.

    Takes one metric or a wildcard seriesList followed by a number N of
    datapoints or a quoted string with a length of time like '1hour' or '5min'
    (See ``from / until`` in the render\_api_ for examples of time formats).
    Graphs the minimum of the preceeding datapoints for each point on the
    graph.

    Example::

      &target=movingMin(Server.instance01.requests,10)
      &target=movingMin(Server.instance*.errors,'5min')

    """
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = abs(delta.seconds + (delta.days * 86400))

    if windowInterval:
        previewSeconds = windowInterval
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'movingMin(%s,"%s")' % (series.name, windowSize)
        else:
            newName = "movingMin(%s,%s)" % (series.name, windowSize)

        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName
        for i in range(windowPoints, len(series)):
            window = series[i - windowPoints:i]
            newSeries.append(safeMin(window))

        result.append(newSeries)

    return result


def movingMax(requestContext, seriesList, windowSize):
    """
    Graphs the moving maximum of a metric (or metrics) over a fixed number of
    past points, or a time interval.

    Takes one metric or a wildcard seriesList followed by a number N of
    datapoints or a quoted string with a length of time like '1hour' or '5min'
    (See ``from / until`` in the render\_api_ for examples of time formats).
    Graphs the maximum of the preceeding datapoints for each point on the
    graph.

    Example::

      &target=movingMax(Server.instance01.requests,10)
      &target=movingMax(Server.instance*.errors,'5min')

    """
    if not seriesList:
        return []
    windowInterval = None
    if isinstance(windowSize, six.string_types):
        delta = parseTimeOffset(windowSize)
        windowInterval = abs(delta.seconds + (delta.days * 86400))

    if windowInterval:
        previewSeconds = windowInterval
    else:
        previewSeconds = max([s.step for s in seriesList]) * int(windowSize)

    # ignore original data and pull new, including our preview
    # data from earlier is needed to calculate the early results
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    result = []

    for series in previewList:
        if windowInterval:
            windowPoints = windowInterval // series.step
        else:
            windowPoints = int(windowSize)

        if isinstance(windowSize, six.string_types):
            newName = 'movingMax(%s,"%s")' % (series.name, windowSize)
        else:
            newName = "movingMax(%s,%s)" % (series.name, windowSize)

        newSeries = TimeSeries(newName, series.start + previewSeconds,
                               series.end, series.step, [])
        newSeries.pathExpression = newName
        for i in range(windowPoints, len(series)):
            window = series[i - windowPoints:i]
            newSeries.append(safeMax(window))

        result.append(newSeries)

    return result


def cumulative(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.

    When a graph is drawn where width of the graph size in pixels is smaller
    than the number of datapoints to be graphed, Graphite consolidates the
    values to prevent line overlap. The cumulative() function changes the
    consolidation function from the default of 'average' to 'sum'. This is
    especially useful in sales graphs, where fractional values make no sense
    and a 'sum' of consolidated values is appropriate.

    Alias for :func:`consolidateBy(series, 'sum')
    <graphite.render.functions.consolidateBy>`

    Example::

        &target=cumulative(Sales.widgets.largeBlue)

    """
    return consolidateBy(requestContext, seriesList, 'sum')


def consolidateBy(requestContext, seriesList, consolidationFunc):
    """
    Takes one metric or a wildcard seriesList and a consolidation function
    name.

    Valid function names are 'sum', 'average', 'min', and 'max'.

    When a graph is drawn where width of the graph size in pixels is smaller
    than the number of datapoints to be graphed, Graphite consolidates the
    values to to prevent line overlap. The consolidateBy() function changes
    the consolidation function from the default of 'average' to one of 'sum',
    'max', or 'min'. This is especially useful in sales graphs, where
    fractional values make no sense and a 'sum' of consolidated values is
    appropriate.

    Example::

        &target=consolidateBy(Sales.widgets.largeBlue, 'sum')
        &target=consolidateBy(Servers.web01.sda1.free_space, 'max')

    """
    for series in seriesList:
        # datalib will throw an exception, so it's not necessary to validate
        # here
        series.consolidationFunc = consolidationFunc
        series.name = 'consolidateBy(%s,"%s")' % (series.name,
                                                  series.consolidationFunc)
        series.pathExpression = series.name
    return seriesList


def derivative(requestContext, seriesList):
    """
    This is the opposite of the integral function. This is useful for taking a
    running total metric and calculating the delta between subsequent data
    points.

    This function does not normalize for periods of time, as a true derivative
    would. Instead see the perSecond() function to calculate a rate of change
    over time.

    Example::

        &target=derivative(company.server.application01.ifconfig.TXPackets)

    Each time you run ifconfig, the RX and TXPackets are higher (assuming there
    is network traffic.) By applying the derivative function, you can get an
    idea of the packets per minute sent or received, even though you're only
    recording the total.
    """
    results = []
    for series in seriesList:
        newValues = []
        prev = None
        for val in series:
            if None in (prev, val):
                newValues.append(None)
                prev = val
                continue
            newValues.append(val - prev)
            prev = val
        newName = "derivative(%s)" % series.name
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def perSecond(requestContext, seriesList, maxValue=None):
    """
    NonNegativeDerivative adjusted for the series time interval
    This is useful for taking a running total metric and showing how many
    requests per second were handled.

    Example::

        &target=perSecond(company.server.application01.ifconfig.TXPackets)

    Each time you run ifconfig, the RX and TXPackets are higher (assuming there
    is network traffic.) By applying the nonNegativeDerivative function, you
    can get an idea of the packets per minute sent or received, even though
    you're only recording the total.
    """
    results = []
    for series in seriesList:
        newValues = []
        prev = None
        step = series.step
        for val in series:
            if prev is None:
                newValues.append(None)
                prev = val
                continue
            if val is None:
                newValues.append(None)
                step = step * 2
                continue

            diff = val - prev
            if diff >= 0:
                newValues.append(diff / step)
            elif maxValue is not None and maxValue >= val:
                newValues.append(((maxValue - prev) + val + 1) / step)
            else:
                newValues.append(None)

            step = series.step
            prev = val
        newName = "perSecond(%s)" % series.name
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def delay(requestContext, seriesList, steps):
    """
    This shifts all samples later by an integer number of steps. This can be
    used for custom derivative calculations, among other things. Note: this
    will pad the early end of the data with None for every step shifted.

    This complements other time-displacement functions such as timeShift and
    timeSlice, in that this function is indifferent about the step intervals
    being shifted.

    Example::

        &target=divideSeries(server.FreeSpace,delay(server.FreeSpace,1))

    This computes the change in server free space as a percentage of the
    previous free space.
    """
    results = []
    for series in seriesList:
        newValues = []
        prev = []
        for val in series:
            if len(prev) < steps:
                newValues.append(None)
                prev.append(val)
                continue
            newValues.append(prev.pop(0))
            prev.append(val)
        newName = "delay(%s,%d)" % (series.name, steps)
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def integral(requestContext, seriesList):
    """
    This will show the sum over time, sort of like a continuous addition
    function. Useful for finding totals or trends in metrics that are
    collected per minute.

    Example::

        &target=integral(company.sales.perMinute)

    This would start at zero on the left side of the graph, adding the sales
    each minute, and show the total sales for the time period selected at the
    right side, (time now, or the time specified by '&until=').
    """
    results = []
    for series in seriesList:
        newValues = []
        current = 0.0
        for val in series:
            if val is None:
                newValues.append(None)
            else:
                current += val
                newValues.append(current)
        newName = "integral(%s)" % series.name
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def integralByInterval(requestContext, seriesList, intervalUnit):
    """
    This will do the same as integral() funcion, except resetting the total
    to 0 at the given time in the parameter "from"
    Useful for finding totals per hour/day/week/..

    Example::

        &target=integralByInterval(company.sales.perMinute,
                                   "1d")&from=midnight-10days

    This would start at zero on the left side of the graph, adding the sales
    each minute, and show the evolution of sales per day during the last 10
    days.

    """
    intervalDuration = int(to_seconds(parseTimeOffset(intervalUnit)))
    startTime = int(epoch(requestContext['startTime']))
    results = []
    for series in seriesList:
        newValues = []
        # current time within series iteration
        currentTime = series.start
        # current accumulated value
        current = 0.0
        for val in series:
            # reset integral value if crossing an interval boundary
            if (
                ((currentTime - startTime) // intervalDuration) !=
                ((currentTime - startTime - series.step) // intervalDuration)
            ):
                current = 0.0
            if val is None:
                # keep previous value since val can be None when resetting
                # current to 0.0
                newValues.append(current)
            else:
                current += val
                newValues.append(current)
            currentTime += series.step
        newName = "integralByInterval(%s,'%s')" % (series.name, intervalUnit)
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def nonNegativeDerivative(requestContext, seriesList, maxValue=None):
    """
    Same as the derivative function above, but ignores datapoints that trend
    down. Useful for counters that increase for a long time, then wrap or
    reset. (Such as if a network interface is destroyed and recreated by
    unloading and re-loading a kernel module, common with USB / WiFi cards.

    Example::

        &target=nonNegativederivative(
            company.server.application01.ifconfig.TXPackets)

    """
    results = []

    for series in seriesList:
        newValues = []
        prev = None

        for val in series:
            if None in (prev, val):
                newValues.append(None)
                prev = val
                continue

            diff = val - prev
            if diff >= 0:
                newValues.append(diff)
            elif maxValue is not None and maxValue >= val:
                newValues.append((maxValue - prev) + val + 1)
            else:
                newValues.append(None)

            prev = val

        newName = "nonNegativeDerivative(%s)" % series.name
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)

    return results


def stacked(requestContext, seriesLists, stackName='__DEFAULT__'):
    """
    Takes one metric or a wildcard seriesList and change them so they are
    stacked. This is a way of stacking just a couple of metrics without having
    to use the stacked area mode (that stacks everything). By means of this a
    mixed stacked and non stacked graph can be made

    It can also take an optional argument with a name of the stack, in case
    there is more than one, e.g. for input and output metrics.

    Example::

        &target=stacked(company.server.application01.ifconfig.TXPackets, 'tx')

    """
    if 'totalStack' in requestContext:
        totalStack = requestContext['totalStack'].get(stackName, [])
    else:
        requestContext['totalStack'] = {}
        totalStack = []
    results = []
    for series in seriesLists:
        newValues = []
        for i in range(len(series)):
            if len(totalStack) <= i:
                totalStack.append(0)

            if series[i] is not None:
                totalStack[i] += series[i]
                newValues.append(totalStack[i])
            else:
                newValues.append(None)

        # Work-around for the case when legend is set
        if stackName == '__DEFAULT__':
            newName = "stacked(%s)" % series.name
        else:
            newName = series.name

        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.options['stacked'] = True
        newSeries.pathExpression = newName
        results.append(newSeries)
    requestContext['totalStack'][stackName] = totalStack
    return results


def areaBetween(requestContext, *seriesLists):
    """
    Draws the vertical area in between the two series in seriesList. Useful for
    visualizing a range such as the minimum and maximum latency for a service.

    areaBetween expects **exactly one argument** that results in exactly two
    series (see example below). The order of the lower and higher values
    series does not matter. The visualization only works when used in
    conjunction with ``areaMode=stacked``.

    Most likely use case is to provide a band within which another metric
    should move. In such case applying an ``alpha()``, as in the second
    example, gives best visual results.

    Example::

      &target=areaBetween(service.latency.{min,max})&areaMode=stacked

      &target=alpha(areaBetween(service.latency.{min,max}),0.3)&areaMode=stacked

    If for instance, you need to build a seriesList, you should use the
    ``group`` function, like so::

      &target=areaBetween(group(minSeries(a.*.min),maxSeries(a.*.max)))
    """
    if len(seriesLists) == 1:
        [seriesLists] = seriesLists
    assert len(seriesLists) == 2, ("areaBetween series argument must "
                                   "reference *exactly* 2 series")
    lower, upper = seriesLists
    if len(lower) == 1:
        [lower] = lower
    if len(upper) == 1:
        [upper] = upper
    lower.options['stacked'] = True
    lower.options['invisible'] = True

    upper.options['stacked'] = True
    lower.name = upper.name = "areaBetween(%s)" % upper.pathExpression
    return [lower, upper]


def aliasSub(requestContext, seriesList, search, replace):
    """
    Runs series names through a regex search/replace.

   Example::

        &target=aliasSub(ip.*TCP*,"^.*TCP(\d+)","\\1")
    """
    try:
        seriesList.name = re.sub(search, replace, seriesList.name)
    except AttributeError:
        for series in seriesList:
            series.name = re.sub(search, replace, series.name)
    return seriesList


def alias(requestContext, seriesList, newName):
    """
    Takes one metric or a wildcard seriesList and a string in quotes.
    Prints the string instead of the metric name in the legend.

    Example::

        &target=alias(Sales.widgets.largeBlue,"Large Blue Widgets")

    """
    try:
        seriesList.name = newName
    except AttributeError:
        for series in seriesList:
            series.name = newName
    return seriesList


def cactiStyle(requestContext, seriesList, system=None, units=None):
    """
    Takes a series list and modifies the aliases to provide column aligned
    output with Current, Max, and Min values in the style of cacti. Optionally
    takes a "system" value to apply unit formatting in the same style as the
    Y-axis, or a "unit" string to append an arbitrary unit suffix.
    NOTE: column alignment only works with monospace fonts such as terminus.

    Example::

        &target=cactiStyle(ganglia.*.net.bytes_out,"si")
        &target=cactiStyle(ganglia.*.net.bytes_out,"si","b")

    """
    def fmt(x):
        if system:
            if units:
                return "%.2f %s" % format_units(x, system=system, units=units)
            else:
                return "%.2f%s" % format_units(x, system=system)
        else:
            if units:
                return "%.2f %s" % (x, units)
            else:
                return "%.2f" % x
    nameLen = max([0] + [len(series.name) for series in seriesList])
    lastLen = max([0] + [len(fmt(int(safeLast(series) or 3)))
                         for series in seriesList]) + 3
    maxLen = max([0] + [len(fmt(int(safeMax(series) or 3)))
                        for series in seriesList]) + 3
    minLen = max([0] + [len(fmt(int(safeMin(series) or 3)))
                        for series in seriesList]) + 3
    for series in seriesList:
        last = safeLast(series)
        maximum = safeMax(series)
        minimum = safeMin(series)
        if last is None:
            last = NAN
        else:
            last = fmt(float(last))

        if maximum is None:
            maximum = NAN
        else:
            maximum = fmt(float(maximum))
        if minimum is None:
            minimum = NAN
        else:
            minimum = fmt(float(minimum))

        series.name = "%*s Current:%*s Max:%*s Min:%*s " % (
            -nameLen, series.name, -lastLen, last,
            -maxLen, maximum, -minLen, minimum)
    return seriesList


def _getFirstPathExpression(name):
    """Returns the first metric path in an expression."""
    tokens = grammar.parseString(name)
    pathExpression = None
    while pathExpression is None:
        if tokens.pathExpression:
            pathExpression = tokens.pathExpression
        elif tokens.expression:
            tokens = tokens.expression
        elif tokens.call:
            tokens = tokens.call.args[0]
        else:
            break
    return pathExpression


def aliasByNode(requestContext, seriesList, *nodes):
    """
    Takes a seriesList and applies an alias derived from one or more "node"
    portion/s of the target name. Node indices are 0 indexed.

    Example::

        &target=aliasByNode(ganglia.*.cpu.load5,1)

    """
    for series in seriesList:
        pathExpression = _getFirstPathExpression(series.name)
        metric_pieces = pathExpression.split('.')
        series.name = '.'.join(metric_pieces[n] for n in nodes)
    return seriesList


def aliasByMetric(requestContext, seriesList):
    """
    Takes a seriesList and applies an alias derived from the base metric name.

    Example::

        &target=aliasByMetric(carbon.agents.graphite.creates)

    """
    return substr(requestContext, seriesList, -1, 0)


def legendValue(requestContext, seriesList, *valueTypes):
    """
    Takes one metric or a wildcard seriesList and a string in quotes.
    Appends a value to the metric name in the legend. Currently one or several
    of: `last`, `avg`, `total`, `min`, `max`. The last argument can be `si`
    (default) or `binary`, in that case values will be formatted in the
    corresponding system.

    Example::

        &target=legendValue(Sales.widgets.largeBlue, 'avg', 'max', 'si')

    """
    valueFuncs = {
        'avg': lambda s: safeDiv(safeSum(s), safeLen(s)),
        'total': safeSum,
        'min': safeMin,
        'max': safeMax,
        'last': safeLast,
    }
    system = None
    if valueTypes[-1] in ('si', 'binary'):
        system = valueTypes[-1]
        valueTypes = valueTypes[:-1]
    for valueType in valueTypes:
        valueFunc = valueFuncs.get(valueType, lambda s: '(?)')
        if system is None:
            for series in seriesList:
                series.name += " (%s: %s)" % (valueType, valueFunc(series))
        else:
            for series in seriesList:
                value = valueFunc(series)
                formatted = None
                if value is not None:
                    formatted = "%.2f%s" % format_units(value, system=system)
                series.name = "%-20s%-5s%-10s" % (series.name, valueType,
                                                  formatted)
    return seriesList


def alpha(requestContext, seriesList, alpha):
    """
    Assigns the given alpha transparency setting to the series. Takes a float
    value between 0 and 1.
    """
    for series in seriesList:
        series.options['alpha'] = alpha
    return seriesList


def color(requestContext, seriesList, theColor):
    """
    Assigns the given color to the seriesList

    Example::

        &target=color(collectd.hostname.cpu.0.user, 'green')
        &target=color(collectd.hostname.cpu.0.system, 'ff0000')
        &target=color(collectd.hostname.cpu.0.idle, 'gray')
        &target=color(collectd.hostname.cpu.0.idle, '6464ffaa')

    """
    for series in seriesList:
        series.color = theColor
    return seriesList


def substr(requestContext, seriesList, start=0, stop=0):
    """
    Takes one metric or a wildcard seriesList followed by 1 or 2 integers.
    Assume that the metric name is a list or array, with each element
    separated by dots. Prints n - length elements of the array (if only one
    integer n is passed) or n - m elements of the array (if two integers n and
    m are passed). The list starts with element 0 and ends with element
    (length - 1).

    Example::

        &target=substr(carbon.agents.hostname.avgUpdateTime,2,4)

    The label would be printed as "hostname.avgUpdateTime".

    """
    for series in seriesList:
        left = series.name.rfind('(') + 1
        right = series.name.find(')')
        if right < 0:
            right = len(series.name)+1
        cleanName = series.name[left:right:].split('.')
        if int(stop) == 0:
            series.name = '.'.join(cleanName[int(start)::])
        else:
            series.name = '.'.join(cleanName[int(start):int(stop):])

        # substr(func(a.b,'c'),1) becomes b instead of b,'c'
        series.name = re.sub(',.*$', '', series.name)
    return seriesList


def logarithm(requestContext, seriesList, base=10):
    """
    Takes one metric or a wildcard seriesList, a base, and draws the y-axis in
    logarithmic format. If base is omitted, the function defaults to base 10.

    Example::

        &target=log(carbon.agents.hostname.avgUpdateTime,2)

    """
    results = []
    for series in seriesList:
        newValues = []
        for val in series:
            if val is None:
                newValues.append(None)
            elif val <= 0:
                newValues.append(None)
            else:
                newValues.append(math.log(val, base))
        newName = "log(%s, %s)" % (series.name, base)
        newSeries = TimeSeries(newName, series.start, series.end, series.step,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)
    return results


def maximumAbove(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by a constant n.
    Draws only the metrics with a maximum value above n.

    Example::

        &target=maximumAbove(system.interface.eth*.packetsSent,1000)

    This would only display interfaces which at one point sent more than
    1000 packets/min.
    """
    results = []
    for series in seriesList:
        val = safeMax(series)
        if val is not None and val > n:
            results.append(series)
    return results


def minimumAbove(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by a constant n.
    Draws only the metrics with a minimum value above n.

    Example::

        &target=minimumAbove(system.interface.eth*.packetsSent,1000)

    This would only display interfaces which always sent more than 1000
    packets/min.
    """
    results = []
    for series in seriesList:
        val = safeMin(series)
        if val is not None and val > n:
            results.append(series)
    return results


def maximumBelow(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by a constant n.
    Draws only the metrics with a maximum value below n.

    Example::

        &target=maximumBelow(system.interface.eth*.packetsSent,1000)

    This would only display interfaces which always sent less than 1000
    packets/min.
    """
    results = []
    for series in seriesList:
        val = safeMax(series)
        if val is None or val <= n:
            results.append(series)
    return results


def minimumBelow(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by a constant n.
    Draws only the metrics with a minimum value below n.

    Example::

        &target=minimumBelow(system.interface.eth*.packetsSent,1000)

    This would only display interfaces which sent at one point less than
    1000 packets/min.
    """
    results = []
    for series in seriesList:
        val = safeMin(series)
        if val is None or val <= n:
            results.append(series)
    return results


def highestCurrent(requestContext, seriesList, n=1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the N metrics with the highest value
    at the end of the time period specified.

    Example::

        &target=highestCurrent(server*.instance*.threads.busy,5)

    Draws the 5 servers with the highest busy threads.

    """
    return sorted(seriesList, key=safeLast)[-n:]


def highestMax(requestContext, seriesList, n=1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.

    Out of all metrics passed, draws only the N metrics with the highest
    maximum value in the time period specified.

    Example::

        &target=highestMax(server*.instance*.threads.busy,5)

    Draws the top 5 servers who have had the most busy threads during the time
    period specified.

    """
    result_list = sorted(seriesList, key=lambda s: safeMax(s))[-n:]
    return sorted(result_list, key=lambda s: max(s), reverse=True)


def lowestCurrent(requestContext, seriesList, n=1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the N metrics with the lowest value
    at the end of the time period specified.

    Example::

        &target=lowestCurrent(server*.instance*.threads.busy,5)

    Draws the 5 servers with the least busy threads right now.

    """
    return sorted(seriesList, key=safeLast)[:n]


def currentAbove(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the metrics whose value is above N
    at the end of the time period specified.

    Example::

        &target=currentAbove(server*.instance*.threads.busy,50)

    Draws the servers with more than 50 busy threads.

    """
    results = []
    for series in seriesList:
        val = safeLast(series)
        if val is not None and val >= n:
            results.append(series)
    return results


def currentBelow(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the    metrics whose value is below N
    at the end of the time period specified.

    Example::

        &target=currentBelow(server*.instance*.threads.busy,3)

    Draws the servers with less than 3 busy threads.

    """
    results = []
    for series in seriesList:
        val = safeLast(series)
        if val is None or val <= n:
            results.append(series)
    return results


def highestAverage(requestContext, seriesList, n=1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the top N metrics with the highest
    average value for the time period specified.

    Example::

        &target=highestAverage(server*.instance*.threads.busy,5)

    Draws the top 5 servers with the highest average value.

    """
    return sorted(seriesList, key=safeAvg)[-n:]


def lowestAverage(requestContext, seriesList, n=1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the bottom N metrics with the lowest
    average value for the time period specified.

    Example::

        &target=lowestAverage(server*.instance*.threads.busy,5)

    Draws the bottom 5 servers with the lowest average value.

    """
    return sorted(seriesList, key=safeAvg)[:n]


def averageAbove(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the metrics with an average value
    above N for the time period specified.

    Example::

        &target=averageAbove(server*.instance*.threads.busy,25)

    Draws the servers with average values above 25.

    """
    results = []
    for series in seriesList:
        val = safeAvg(series)
        if val is not None and val >= n:
            results.append(series)
    return results


def averageBelow(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Out of all metrics passed, draws only the metrics with an average value
    below N for the time period specified.

    Example::

        &target=averageBelow(server*.instance*.threads.busy,25)

    Draws the servers with average values below 25.

    """
    results = []
    for series in seriesList:
        val = safeAvg(series)
        if val is None or val <= n:
            results.append(series)
    return results


def _getPercentile(points, n, interpolate=False):
    """
    Percentile is calculated using the method outlined in the NIST Engineering
    Statistics Handbook:
    http://www.itl.nist.gov/div898/handbook/prc/section2/prc252.htm
    """
    sortedPoints = sorted(not_none(points))
    if len(sortedPoints) == 0:
        return None
    fractionalRank = (n/100.0) * (len(sortedPoints) + 1)
    rank = int(fractionalRank)
    rankFraction = fractionalRank - rank

    if not interpolate:
        rank += int(math.ceil(rankFraction))

    if rank == 0:
        percentile = sortedPoints[0]
    elif rank - 1 == len(sortedPoints):
        percentile = sortedPoints[-1]
    else:
        percentile = sortedPoints[rank - 1]  # Adjust for 0-index

    if interpolate:
        if rank != len(sortedPoints):  # if a next value exists
            nextValue = sortedPoints[rank]
            percentile = percentile + rankFraction * (nextValue - percentile)

    return percentile


def nPercentile(requestContext, seriesList, n):
    """Returns n-percent of each series in the seriesList."""
    assert n, 'The requested percent is required to be greater than 0'

    results = []
    for s in seriesList:
        # Create a sorted copy of the TimeSeries excluding None values in the
        # values list.
        s_copy = TimeSeries(s.name, s.start, s.end, s.step,
                            sorted(not_none(s)))
        if not s_copy:
            continue    # Skip this series because it is empty.

        perc_val = _getPercentile(s_copy, n)
        if perc_val is not None:
            name = 'nPercentile(%s, %g)' % (s_copy.name, n)
            point_count = int((s.end - s.start)/s.step)
            perc_series = TimeSeries(name, s_copy.start, s_copy.end,
                                     s_copy.step, [perc_val] * point_count)
            perc_series.pathExpression = name
            results.append(perc_series)
    return results


def averageOutsidePercentile(requestContext, seriesList, n):
    """
    Removes functions lying inside an average percentile interval
    """
    averages = [safeAvg(s) for s in seriesList]

    if n < 50:
        n = 100 - n

    lowPercentile = _getPercentile(averages, 100 - n)
    highPercentile = _getPercentile(averages, n)

    return [s for s in seriesList
            if not lowPercentile < safeAvg(s) < highPercentile]


def removeBetweenPercentile(requestContext, seriesList, n):
    """
    Removes lines who do not have an value lying in the x-percentile of all
    the values at a moment
    """
    if n < 50:
        n = 100 - n

    transposed = list(zip_longest(*seriesList))

    lowPercentiles = [_getPercentile(col, 100-n) for col in transposed]
    highPercentiles = [_getPercentile(col, n) for col in transposed]

    return [l for l in seriesList
            if sum([not lowPercentiles[index] < val < highPercentiles[index]
                    for index, val in enumerate(l)]) > 0]


def removeAbovePercentile(requestContext, seriesList, n):
    """
    Removes data above the nth percentile from the series or list of series
    provided. Values above this percentile are assigned a value of None.
    """
    for s in seriesList:
        s.name = 'removeAbovePercentile(%s, %g)' % (s.name, n)
        s.pathExpression = s.name
        try:
            percentile = nPercentile(requestContext, [s], n)[0][0]
        except IndexError:
            continue
        for index, val in enumerate(s):
            if val is None:
                continue
            if val > percentile:
                s[index] = None

    return seriesList


def removeAboveValue(requestContext, seriesList, n):
    """
    Removes data above the given threshold from the series or list of series
    provided. Values above this threshold are assigned a value of None.
    """
    for s in seriesList:
        s.name = 'removeAboveValue(%s, %g)' % (s.name, n)
        s.pathExpression = s.name
        for (index, val) in enumerate(s):
            if val is None:
                continue
            if val > n:
                s[index] = None

    return seriesList


def removeBelowPercentile(requestContext, seriesList, n):
    """
    Removes data below the nth percentile from the series or list of series
    provided. Values below this percentile are assigned a value of None.
    """
    for s in seriesList:
        s.name = 'removeBelowPercentile(%s, %g)' % (s.name, n)
        s.pathExpression = s.name
        try:
            percentile = nPercentile(requestContext, [s], n)[0][0]
        except IndexError:
            continue
        for (index, val) in enumerate(s):
            if val is None:
                continue
            if val < percentile:
                s[index] = None

    return seriesList


def removeBelowValue(requestContext, seriesList, n):
    """
    Removes data below the given threshold from the series or list of series
    provided. Values below this threshold are assigned a value of None.
    """
    for s in seriesList:
        s.name = 'removeBelowValue(%s, %g)' % (s.name, n)
        s.pathExpression = s.name
        for index, val in enumerate(s):
            if val is None:
                continue
            if val < n:
                s[index] = None

    return seriesList


def limit(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.

    Only draw the first N metrics. Useful when testing a wildcard in a
    metric.

    Example::

        &target=limit(server*.instance*.memory.free,5)

    Draws only the first 5 instance's memory free.

    """
    return seriesList[0:n]


def paddedName(name):
    return re.sub("(\d+)", lambda x: "{0:010}".format(int(x.group(0))), name)


def sortByName(requestContext, seriesList, natural=False):
    """
    Takes one metric or a wildcard seriesList.

    Sorts the list of metrics by the metric name using either alphabetical
    order or natural sorting. Natural sorting allows names containing numbers
    to be sorted more naturally, e.g:

    - Alphabetical sorting: server1, server11, server12, server2
    - Natural sorting: server1, server2, server11, server12

    """
    if natural:
        return list(sorted(seriesList, key=lambda x: paddedName(x.name)))
    else:
        return list(sorted(seriesList, key=lambda x: x.name))


def sortByTotal(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.

    Sorts the list of metrics by the sum of values across the time period
    specified.
    """
    return list(sorted(seriesList, key=safeSum, reverse=True))


def sortByMaxima(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.

    Sorts the list of metrics by the maximum value across the time period
    specified.    Useful with the &areaMode=all parameter, to keep the
    lowest value lines visible.

    Example::

        &target=sortByMaxima(server*.instance*.memory.free)

    """
    return list(sorted(seriesList, key=max))


def sortByMinima(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.

    Sorts the list of metrics by the lowest value across the time period
    specified.

    Example::

        &target=sortByMinima(server*.instance*.memory.free)

    """
    return list(sorted(seriesList, key=min))


def useSeriesAbove(requestContext, seriesList, value, search, replace):
    """
    Compares the maximum of each series against the given `value`. If the
    series maximum is greater than `value`, the regular expression search and
    replace is applied against the series name to plot a related metric.

    e.g. given useSeriesAbove(ganglia.metric1.reqs,10,'reqs','time'),
    the response time metric will be plotted only when the maximum value of the
    corresponding request/s metric is > 10

    Example::

        &target=useSeriesAbove(ganglia.metric1.reqs,10,"reqs","time")
    """
    newSeries = []

    for series in seriesList:
        newname = re.sub(search, replace, series.name)
        if safeMax(series) > value:
            n = evaluateTarget(requestContext, newname)
            if n is not None and len(n) > 0:
                newSeries.append(n[0])

    return newSeries


def fallbackSeries(requestContext, seriesList, fallback):
    """
    Takes a wildcard seriesList, and a second fallback metric.
    If the wildcard does not match any series, draws the fallback metric.

    Example::

        &target=fallbackSeries(server*.requests_per_second, constantLine(0))

    Draws a 0 line when server metric does not exist.

    """
    if len(seriesList) > 0:
        return seriesList
    else:
        return fallback


def mostDeviant(requestContext, seriesList, n):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Draws the N most deviant metrics.
    To find the deviants, the standard deviation (sigma) of each series
    is taken and ranked. The top N standard deviations are returned.

    Example::

        &target=mostDeviant(server*.instance*.memory.free, 5)

    Draws the 5 instances furthest from the average memory free.
    """

    deviants = []
    for series in seriesList:
        mean = safeAvg(series)
        if mean is None:
            continue
        square_sum = sum([(value - mean) ** 2 for value in series
                          if value is not None])
        sigma = safeDiv(square_sum, safeLen(series))
        if sigma is None:
            continue
        deviants.append((sigma, series))
    return [series for sig, series in sorted(deviants,  # sort by sigma
                                             key=itemgetter(0),
                                             reverse=True)][:n]


def stdev(requestContext, seriesList, points, windowTolerance=0.1):
    """
    Takes one metric or a wildcard seriesList followed by an integer N.
    Draw the Standard Deviation of all metrics passed for the past N
    datapoints. If the ratio of null points in the window is greater than
    windowTolerance, skip the calculation. The default for windowTolerance is
    0.1 (up to 10% of points in the window can be missing). Note that if this
    is set to 0.0, it will cause large gaps in the output anywhere a single
    point is missing.

    Example::

        &target=stdev(server*.instance*.threads.busy,30)
        &target=stdev(server*.instance*.cpu.system,30,0.0)

    """

    # For this we take the standard deviation in terms of the moving average
    # and the moving average of series squares.
    for seriesIndex, series in enumerate(seriesList):
        stdevSeries = TimeSeries("stdev(%s,%d)" % (series.name, int(points)),
                                 series.start, series.end, series.step, [])
        stdevSeries.pathExpression = "stdev(%s,%d)" % (series.name,
                                                       int(points))

        validPoints = 0
        currentSum = 0
        currentSumOfSquares = 0
        for index, newValue in enumerate(series):
            # Mark whether we've reached our window size - dont drop points
            # out otherwise
            if index < points:
                bootstrapping = True
                droppedValue = None
            else:
                bootstrapping = False
                droppedValue = series[index - points]

            # Track non-None points in window
            if not bootstrapping and droppedValue is not None:
                validPoints -= 1
            if newValue is not None:
                validPoints += 1

            # Remove the value that just dropped out of the window
            if not bootstrapping and droppedValue is not None:
                currentSum -= droppedValue
                currentSumOfSquares -= droppedValue**2

            # Add in the value that just popped in the window
            if newValue is not None:
                currentSum += newValue
                currentSumOfSquares += newValue**2

            if (
                validPoints > 0 and
                float(validPoints) / points >= windowTolerance
            ):
                try:
                    deviation = math.sqrt(validPoints * currentSumOfSquares -
                                          currentSum**2) / validPoints
                except ValueError:
                    deviation = None
                stdevSeries.append(deviation)
            else:
                stdevSeries.append(None)

        seriesList[seriesIndex] = stdevSeries

    return seriesList


def secondYAxis(requestContext, seriesList):
    """
    Graph the series on the secondary Y axis.
    """
    for series in seriesList:
        series.options['secondYAxis'] = True
        series.name = 'secondYAxis(%s)' % series.name
    return seriesList


def holtWintersIntercept(alpha, actual, last_season, last_intercept,
                         last_slope):
    return (alpha * (actual - last_season) +
            (1 - alpha) * (last_intercept + last_slope))


def holtWintersSlope(beta, intercept, last_intercept, last_slope):
    return beta * (intercept - last_intercept) + (1 - beta) * last_slope


def holtWintersSeasonal(gamma, actual, intercept, last_season):
    return gamma * (actual - intercept) + (1 - gamma) * last_season


def holtWintersDeviation(gamma, actual, prediction, last_seasonal_dev):
    if prediction is None:
        prediction = 0
    return (gamma * math.fabs(actual - prediction) +
            (1 - gamma) * last_seasonal_dev)


def holtWintersAnalysis(series):
    alpha = gamma = 0.1
    beta = 0.0035
    # season is currently one day
    season_length = (24 * 60 * 60) // series.step
    intercept = 0
    slope = 0
    intercepts = []
    slopes = []
    seasonals = []
    predictions = []
    deviations = []

    def getLastSeasonal(i):
        j = i - season_length
        if j >= 0:
            return seasonals[j]
        return 0

    def getLastDeviation(i):
        j = i - season_length
        if j >= 0:
            return deviations[j]
        return 0

    last_seasonal = 0
    last_seasonal_dev = 0
    next_last_seasonal = 0
    next_pred = None

    for i, actual in enumerate(series):
        if actual is None:
            # missing input values break all the math
            # do the best we can and move on
            intercepts.append(None)
            slopes.append(0)
            seasonals.append(0)
            predictions.append(next_pred)
            deviations.append(0)
            next_pred = None
            continue

        if i == 0:
            last_intercept = actual
            last_slope = 0
            # seed the first prediction as the first actual
            prediction = actual
        else:
            last_intercept = intercepts[-1]
            last_slope = slopes[-1]
            if last_intercept is None:
                last_intercept = actual
            prediction = next_pred

        last_seasonal = getLastSeasonal(i)
        next_last_seasonal = getLastSeasonal(i+1)
        last_seasonal_dev = getLastDeviation(i)

        intercept = holtWintersIntercept(alpha, actual, last_seasonal,
                                         last_intercept, last_slope)
        slope = holtWintersSlope(beta, intercept, last_intercept, last_slope)
        seasonal = holtWintersSeasonal(gamma, actual, intercept, last_seasonal)
        next_pred = intercept + slope + next_last_seasonal
        deviation = holtWintersDeviation(gamma, actual, prediction,
                                         last_seasonal_dev)

        intercepts.append(intercept)
        slopes.append(slope)
        seasonals.append(seasonal)
        predictions.append(prediction)
        deviations.append(deviation)

    # make the new forecast series
    forecastName = "holtWintersForecast(%s)" % series.name
    forecastSeries = TimeSeries(forecastName, series.start, series.end,
                                series.step, predictions)
    forecastSeries.pathExpression = forecastName

    # make the new deviation series
    deviationName = "holtWintersDeviation(%s)" % series.name
    deviationSeries = TimeSeries(deviationName, series.start, series.end,
                                 series.step, deviations)
    deviationSeries.pathExpression = deviationName

    results = {'predictions': forecastSeries,
               'deviations': deviationSeries,
               'intercepts': intercepts,
               'slopes': slopes,
               'seasonals': seasonals}
    return results


def holtWintersForecast(requestContext, seriesList):
    """
    Performs a Holt-Winters forecast using the series as input data. Data from
    one week previous to the series is used to bootstrap the initial forecast.
    """
    previewSeconds = 7 * 86400  # 7 days
    # ignore original data and pull new, including our preview
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    results = []
    for series in previewList:
        analysis = holtWintersAnalysis(series)
        predictions = analysis['predictions']
        windowPoints = previewSeconds // predictions.step
        result = TimeSeries("holtWintersForecast(%s)" % series.name,
                            predictions.start + previewSeconds,
                            predictions.end, predictions.step,
                            predictions[windowPoints:])
        result.pathExpression = result.name
        results.append(result)
    return results


def holtWintersConfidenceBands(requestContext, seriesList, delta=3):
    """
    Performs a Holt-Winters forecast using the series as input data and plots
    upper and lower bands with the predicted forecast deviations.
    """
    previewSeconds = 7 * 86400  # 7 days
    # ignore original data and pull new, including our preview
    newContext = requestContext.copy()
    newContext['startTime'] = (requestContext['startTime'] -
                               timedelta(seconds=previewSeconds))
    previewList = evaluateTokens(newContext, requestContext['args'][0])
    results = []
    for series in previewList:
        analysis = holtWintersAnalysis(series)

        data = analysis['predictions']
        windowPoints = previewSeconds // data.step
        forecast = TimeSeries(data.name, data.start + previewSeconds,
                              data.end, data.step, data[windowPoints:])
        forecast.pathExpression = data.pathExpression

        data = analysis['deviations']
        windowPoints = previewSeconds // data.step
        deviation = TimeSeries(data.name, data.start + previewSeconds,
                               data.end, data.step, data[windowPoints:])
        deviation.pathExpression = data.pathExpression

        seriesLength = len(forecast)
        i = 0
        upperBand = list()
        lowerBand = list()
        while i < seriesLength:
            forecast_item = forecast[i]
            deviation_item = deviation[i]
            i = i + 1
            if forecast_item is None or deviation_item is None:
                upperBand.append(None)
                lowerBand.append(None)
            else:
                scaled_deviation = delta * deviation_item
                upperBand.append(forecast_item + scaled_deviation)
                lowerBand.append(forecast_item - scaled_deviation)

        upperName = "holtWintersConfidenceUpper(%s)" % series.name
        lowerName = "holtWintersConfidenceLower(%s)" % series.name
        upperSeries = TimeSeries(upperName, forecast.start, forecast.end,
                                 forecast.step, upperBand)
        lowerSeries = TimeSeries(lowerName, forecast.start, forecast.end,
                                 forecast.step, lowerBand)
        upperSeries.pathExpression = series.pathExpression
        lowerSeries.pathExpression = series.pathExpression
        results.append(lowerSeries)
        results.append(upperSeries)
    return results


def holtWintersAberration(requestContext, seriesList, delta=3):
    """
    Performs a Holt-Winters forecast using the series as input data and plots
    the positive or negative deviation of the series data from the forecast.
    """
    results = []
    for series in seriesList:
        confidenceBands = holtWintersConfidenceBands(requestContext, [series],
                                                     delta)
        lowerBand = confidenceBands[0]
        upperBand = confidenceBands[1]
        aberration = list()
        for i, actual in enumerate(series):
            if actual is None:
                aberration.append(0)
            elif upperBand[i] is not None and actual > upperBand[i]:
                aberration.append(actual - upperBand[i])
            elif lowerBand[i] is not None and actual < lowerBand[i]:
                aberration.append(actual - lowerBand[i])
            else:
                aberration.append(0)

        newName = "holtWintersAberration(%s)" % series.name
        results.append(TimeSeries(newName, series.start, series.end,
                                  series.step, aberration))
    return results


def holtWintersConfidenceArea(requestContext, seriesList, delta=3):
    """
    Performs a Holt-Winters forecast using the series as input data and plots
    the area between the upper and lower bands of the predicted forecast
    deviations.
    """
    bands = holtWintersConfidenceBands(requestContext, seriesList, delta)
    results = areaBetween(requestContext, bands)
    for series in results:
        series.name = series.name.replace('areaBetween',
                                          'holtWintersConfidenceArea')
    return results


def linearRegressionAnalysis(series):
    """
    Returns factor and offset of linear regression function by least
    squares method.

    """
    n = safeLen(series)
    sumI = sum([i for i, v in enumerate(series) if v is not None])
    sumV = sum([v for i, v in enumerate(series) if v is not None])
    sumII = sum([i * i for i, v in enumerate(series) if v is not None])
    sumIV = sum([i * v for i, v in enumerate(series) if v is not None])
    denominator = float(n * sumII - sumI * sumI)
    if denominator == 0:
        return None
    else:
        factor = (n * sumIV - sumI * sumV) / denominator / series.step
        offset = sumII * sumV - sumIV * sumI
        offset = offset / denominator - factor * series.start
        return factor, offset


def linearRegression(requestContext, seriesList, startSourceAt=None,
                     endSourceAt=None):
    """
    Graphs the liner regression function by least squares method.

    Takes one metric or a wildcard seriesList, followed by a quoted string
    with the time to start the line and another quoted string with the time
    to end the line. The start and end times are inclusive (default range is
    from to until). See ``from / until`` in the render\_api_ for examples of
    time formats. Datapoints in the range is used to regression.

    Example::

        &target=linearRegression(Server.instance01.threads.busy,'-1d')
        &target=linearRegression(Server.instance*.threads.busy,
                                 "00:00 20140101","11:59 20140630")

    """
    from .app import evaluateTarget
    results = []
    sourceContext = requestContext.copy()
    if startSourceAt is not None:
        sourceContext['startTime'] = parseATTime(startSourceAt)
    if endSourceAt is not None:
        sourceContext['endTime'] = parseATTime(endSourceAt)

    sourceList = []
    for series in seriesList:
        source = evaluateTarget(sourceContext, series.pathExpression)
        sourceList.extend(source)

    for source, series in zip(sourceList, seriesList):
        newName = 'linearRegression(%s, %s, %s)' % (
            series.name,
            int(epoch(sourceContext['startTime'])),
            int(epoch(sourceContext['endTime'])))
        forecast = linearRegressionAnalysis(source)
        if forecast is None:
            continue
        factor, offset = forecast
        values = [offset + (series.start + i * series.step) * factor
                  for i in range(len(series))]
        newSeries = TimeSeries(newName, series.start, series.end,
                               series.step, values)
        newSeries.pathExpression = newSeries.name
        results.append(newSeries)
    return results


def drawAsInfinite(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList.
    If the value is zero, draw the line at 0. If the value is above zero, draw
    the line at infinity. If the value is null or less than zero, do not draw
    the line.

    Useful for displaying on/off metrics, such as exit codes. (0 = success,
    anything else = failure.)

    Example::

        drawAsInfinite(Testing.script.exitCode)

    """
    for series in seriesList:
        series.options['drawAsInfinite'] = True
        series.name = 'drawAsInfinite(%s)' % series.name
    return seriesList


def lineWidth(requestContext, seriesList, width):
    """
    Takes one metric or a wildcard seriesList, followed by a float F.

    Draw the selected metrics with a line width of F, overriding the default
    value of 1, or the &lineWidth=X.X parameter.

    Useful for highlighting a single metric out of many, or having multiple
    line widths in one graph.

    Example::

        &target=lineWidth(server01.instance01.memory.free,5)

    """
    for series in seriesList:
        series.options['lineWidth'] = width
    return seriesList


def dashed(requestContext, seriesList, dashLength=5):
    """
    Takes one metric or a wildcard seriesList, followed by a float F.

    Draw the selected metrics with a dotted line with segments of length F
    If omitted, the default length of the segments is 5.0

    Example::

        &target=dashed(server01.instance01.memory.free,2.5)

    """
    for series in seriesList:
        series.name = 'dashed(%s, %g)' % (series.name, dashLength)
        series.options['dashed'] = dashLength
    return seriesList


def timeStack(requestContext, seriesList, timeShiftUnit, timeShiftStart,
              timeShiftEnd):
    """
    Takes one metric or a wildcard seriesList, followed by a quoted string
    with the length of time (See ``from / until`` in the render\_api_ for
    examples of time formats). Also takes a start multiplier and end
    multiplier for the length of time-

    Create a seriesList which is composed the original metric series stacked
    with time shifts starting time shifts from the start multiplier through
    the end multiplier.

    Useful for looking at history, or feeding into averageSeries or
    stddevSeries.

    Example::

        # create a series for today and each of the previous 7 days
        &target=timeStack(Sales.widgets.largeBlue,"1d",0,7)
    """
    # Default to negative. parseTimeOffset defaults to +
    if timeShiftUnit[0].isdigit():
        timeShiftUnit = '-' + timeShiftUnit
    delta = parseTimeOffset(timeShiftUnit)
    # if len(seriesList) > 1, they will all have the same pathExpression,
    # which is all we care about.
    series = seriesList[0]
    results = []
    timeShiftStartint = int(timeShiftStart)
    timeShiftEndint = int(timeShiftEnd)

    for shft in range(timeShiftStartint, timeShiftEndint):
        myContext = requestContext.copy()
        innerDelta = delta * shft
        myContext['startTime'] = requestContext['startTime'] + innerDelta
        myContext['endTime'] = requestContext['endTime'] + innerDelta
        for shiftedSeries in evaluateTarget(myContext, series.pathExpression):
            shiftedSeries.name = 'timeShift(%s, %s, %s)' % (shiftedSeries.name,
                                                            timeShiftUnit,
                                                            shft)
            shiftedSeries.pathExpression = shiftedSeries.name
            shiftedSeries.start = series.start
            shiftedSeries.end = series.end
            results.append(shiftedSeries)

    return results


def localDST(dt):
    return time.localtime(time.mktime(dt.timetuple())).tm_isdst


def timeShift(requestContext, seriesList, timeShift, resetEnd=True,
              alignDST=False):
    """
    Takes one metric or a wildcard seriesList, followed by a quoted string
    with the length of time (See ``from / until`` in the render\_api_ for
    examples of time formats).

    Draws the selected metrics shifted in time. If no sign is given, a minus
    sign ( - ) is implied which will shift the metric back in time. If a plus
    sign ( + ) is given, the metric will be shifted forward in time.

    Will reset the end date range automatically to the end of the base stat
    unless resetEnd is False. Example case is when you timeshift to last week
    and have the graph date range set to include a time in the future, will
    limit this timeshift to pretend ending at the current time. If resetEnd is
    False, will instead draw full range including future time.

    Because time is shifted by a fixed number of seconds, comparing a time
    period with DST to a time period without DST, and vice-versa, will result
    in an apparent misalignment. For example, 8am might be overlaid with 7am.
    To compensate for this, use the alignDST option.

    Useful for comparing a metric against itself at a past periods or
    correcting data stored at an offset.

    Example::

        &target=timeShift(Sales.widgets.largeBlue,"7d")
        &target=timeShift(Sales.widgets.largeBlue,"-7d")
        &target=timeShift(Sales.widgets.largeBlue,"+1h")

    """
    # Default to negative. parseTimeOffset defaults to +
    if timeShift[0].isdigit():
        timeShift = '-' + timeShift
    delta = parseTimeOffset(timeShift)
    myContext = requestContext.copy()
    myContext['startTime'] = requestContext['startTime'] + delta
    myContext['endTime'] = requestContext['endTime'] + delta

    if alignDST:
        reqStartDST = localDST(requestContext['startTime'])
        reqEndDST = localDST(requestContext['endTime'])
        myStartDST = localDST(myContext['startTime'])
        myEndDST = localDST(myContext['endTime'])

        dstOffset = timedelta(hours=0)
        # If the requestContext is entirely in DST, and we are entirely
        # NOT in DST
        if (
            (reqStartDST and reqEndDST) and
            (not myStartDST and not myEndDST)
        ):
            dstOffset = timedelta(hours=1)
        # Or if the requestContext is entirely NOT in DST, and we are
        # entirely in DST
        elif (
            (not reqStartDST and not reqEndDST) and
            (myStartDST and myEndDST)
        ):
            dstOffset = timedelta(hours=-1)
        # Otherwise, we don't do anything, because it would be visually
        # confusing
        myContext['startTime'] += dstOffset
        myContext['endTime'] += dstOffset

    results = []
    if not seriesList:
        return results

    # if len(seriesList) > 1, they will all have the same pathExpression,
    # which is all we care about.
    series = seriesList[0]

    for shiftedSeries in evaluateTarget(myContext, series.pathExpression):
        shiftedSeries.name = 'timeShift(%s, %s)' % (shiftedSeries.name,
                                                    timeShift)
        if resetEnd:
            shiftedSeries.end = series.end
        else:
            shiftedSeries.end = (
                shiftedSeries.end - shiftedSeries.start + series.start)
        shiftedSeries.start = series.start
        results.append(shiftedSeries)

    return results


def timeSlice(requestContext, seriesList, startSliceAt, endSliceAt='now'):
    """
    Takes one metric or a wildcard metric, followed by a quoted
    string with the time to start the line and another quoted string
    with the time to end the line. The start and end times are
    inclusive. See ``from / until`` in the render api for examples of
    time formats.

    Useful for filtering out a part of a series of data from a wider
    range of data.

    Example::

        &target=timeSlice(network.core.port1,"00:00 20140101","11:59 20140630")
        &target=timeSlice(network.core.port1,"12:00 20140630","now")
    """
    results = []
    start = epoch(parseATTime(startSliceAt))
    end = epoch(parseATTime(endSliceAt))

    for slicedSeries in seriesList:
        slicedSeries.name = 'timeSlice(%s, %s, %s)' % (slicedSeries.name,
                                                       int(start), int(end))
        curr = epoch(requestContext["startTime"])
        for i, v in enumerate(slicedSeries):
            if v is None or curr < start or curr > end:
                slicedSeries[i] = None
            curr += slicedSeries.step
        results.append(slicedSeries)
    return results


def constantLine(requestContext, value):
    """
    Takes a float F.

    Draws a horizontal line at value F across the graph.

    Example::

        &target=constantLine(123.456)

    """
    name = "constantLine(%s)" % str(value)
    start = int(epoch(requestContext['startTime']))
    end = int(epoch(requestContext['endTime']))
    step = int((end - start) / 2.0)
    series = TimeSeries(str(value), start, end, step, [value, value, value])
    series.pathExpression = name
    return [series]


def aggregateLine(requestContext, seriesList, func='avg'):
    """
    Takes a metric or wildcard seriesList and draws a horizontal line
    based on the function applied to each series.

    Note: By default, the graphite renderer consolidates data points by
    averaging data points over time. If you are using the 'min' or 'max'
    function for aggregateLine, this can cause an unusual gap in the
    line drawn by this function and the data itself. To fix this, you
    should use the consolidateBy() function with the same function
    argument you are using for aggregateLine. This will ensure that the
    proper data points are retained and the graph should line up
    correctly.

    Example::

        &target=aggregateLine(server01.connections.total, 'avg')
        &target=aggregateLine(server*.connections.total, 'avg')

    """
    t_funcs = {'avg': safeAvg, 'min': safeMin, 'max': safeMax}

    if func not in t_funcs:
        raise ValueError("Invalid function %s" % func)

    results = []
    for series in seriesList:
        value = t_funcs[func](series)
        if value is not None:
            name = 'aggregateLine(%s, %g)' % (series.name, value)
        else:
            name = 'aggregateLine(%s, None)' % (series.name)

        [series] = constantLine(requestContext, value)
        series.name = name
        series.pathExpression = series.name
        results.append(series)
    return results


def verticalLine(requestContext, ts, label=None, color=None):
    """
    Takes a timestamp string ts.

    Draws a vertical line at the designated timestamp with optional
    'label' and 'color'. Supported timestamp formats include both
    relative (e.g. -3h) and absolute (e.g. 16:00_20110501) strings,
    such as those used with ``from`` and ``until`` parameters. When
    set, the 'label' will appear in the graph legend.

    Note: Any timestamps defined outside the requested range will
    raise a 'ValueError' exception.

    Example::

        &target=verticalLine("12:3420131108","event","blue")
        &target=verticalLine("16:00_20110501","event")
        &target=verticalLine("-5mins")

    """
    ts = int(epoch(parseATTime(ts, requestContext['tzinfo'])))
    start = int(epoch(requestContext['startTime']))
    end = int(epoch(requestContext['endTime']))
    if ts < start:
        raise ValueError("verticalLine(): timestamp %s exists "
                         "before start of range" % ts)
    elif ts > end:
        raise ValueError("verticalLine(): timestamp %s exists "
                         "after end of range" % ts)
    start = end = ts
    step = 1.0
    series = TimeSeries(label, start, end, step, [1.0, 1.0])
    series.options['drawAsInfinite'] = True
    if color:
        series.color = color
    return [series]


def threshold(requestContext, value, label=None, color=None):
    """
    Takes a float F, followed by a label (in double quotes) and a color.
    (See ``bgcolor`` in the render\_api_ for valid color names & formats.)

    Draws a horizontal line at value F across the graph.

    Example::

        &target=threshold(123.456, "omgwtfbbq", "red")

    """
    [series] = constantLine(requestContext, value)
    if label:
        series.name = label
    if color:
        series.color = color
    return [series]


def transformNull(requestContext, seriesList, default=0, referenceSeries=None):
    """
    Takes a metric or wildcard seriesList and replaces null values with
    the value specified by `default`.  The value 0 used if not specified.
    The optional referenceSeries, if specified, is a metric or wildcard
    series list that governs which time intervals nulls should be replaced.
    If specified, nulls are replaced only in intervals where a non-null is
    found for the same interval in any of referenceSeries. This method
    compliments the drawNullAsZero function in graphical mode, but also
    works in text-only mode.

    Example::

        &target=transformNull(webapp.pages.*.views,-1)

    This would take any page that didn't have values and supply negative 1 as
    a default. Any other numeric value may be used as well.
    """
    def transform(v, d):
        if v is None:
            return d
        else:
            return v

    if referenceSeries:
        defaults = [default if any(v is not None for v in x) else None
                    for x in zip_longest(*referenceSeries)]
    else:
        defaults = None

    for series in seriesList:
        if referenceSeries:
            series.name = "transformNull(%s,%g,referenceSeries)" % (
                series.name, default)
        else:
            series.name = "transformNull(%s,%g)" % (series.name, default)
        series.pathExpression = series.name
        if defaults:
            values = [transform(v, d) for v, d in zip_longest(series,
                                                              defaults)]
        else:
            values = [transform(v, default) for v in series]
        series.extend(values)
        del series[:len(values)]
    return seriesList


def isNonNull(requestContext, seriesList):
    """
    Takes a metric or wild card seriesList and counts up how many
    non-null values are specified. This is useful for understanding
    which metrics have data at a given point in time (ie, to count
    which servers are alive).

    Example::

        &target=isNonNull(webapp.pages.*.views)

    Returns a seriesList where 1 is specified for non-null values, and
    0 is specified for null values.
    """

    def transform(v):
        if v is None:
            return 0
        else:
            return 1

    for series in seriesList:
        series.name = "isNonNull(%s)" % (series.name)
        series.pathExpression = series.name
        values = [transform(v) for v in series]
        series.extend(values)
        del series[:len(values)]
    return seriesList


def identity(requestContext, name, step=60):
    """
    Identity function:
    Returns datapoints where the value equals the timestamp of the datapoint.
    Useful when you have another series where the value is a timestamp, and
    you want to compare it to the time of the datapoint, to render an age

    Example::

        &target=identity("The.time.series")

    This would create a series named "The.time.series" that contains points
    where x(t) == t.

    Accepts optional second argument as 'step' parameter (default step is
    60 sec)
    """
    start = int(epoch(requestContext["startTime"]))
    end = int(epoch(requestContext["endTime"]))
    values = range(start, end, step)
    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = 'identity("%s")' % name

    return [series]


def countSeries(requestContext, *seriesLists):
    """
    Draws a horizontal line representing the number of nodes found in the
    seriesList.

    Example::

        &target=countSeries(carbon.agents.*.*)

    """
    if not seriesLists or not any(seriesLists):
        series = constantLine(requestContext, 0).pop()
        series.pathExpression = "countSeries()"
    else:
        seriesList, start, end, step = normalize(seriesLists)
        name = "countSeries(%s)" % formatPathExpressions(seriesList)
        values = (int(len(row)) for row in zip_longest(*seriesList))
        series = TimeSeries(name, start, end, step, values)
        series.pathExpression = name

    return [series]


def group(requestContext, *seriesLists):
    """
    Takes an arbitrary number of seriesLists and adds them to a single
    seriesList. This is used to pass multiple seriesLists to a function which
    only takes one.
    """
    seriesGroup = []
    for s in seriesLists:
        seriesGroup.extend(s)

    return seriesGroup


def mapSeries(requestContext, seriesList, mapNode):
    """
    Short form: ``map()``.

    Takes a seriesList and maps it to a list of sub-seriesList. Each
    sub-seriesList has the given mapNode in common.

    Example (note: This function is not very useful alone. It should be used
    with :py:func:`reduceSeries`)::

        mapSeries(servers.*.cpu.*,1) =>
            [
                servers.server1.cpu.*,
                servers.server2.cpu.*,
                ...
                servers.serverN.cpu.*
            ]
    """
    metaSeries = {}
    keys = []
    for series in seriesList:
        key = series.name.split(".")[mapNode]
        if key not in metaSeries:
            metaSeries[key] = [series]
            keys.append(key)
        else:
            metaSeries[key].append(series)
    return [metaSeries[k] for k in keys]


def reduceSeries(requestContext, seriesLists, reduceFunction, reduceNode,
                 *reduceMatchers):
    """
    Short form: ``reduce()``.

    Takes a list of seriesLists and reduces it to a list of series by means of
    the reduceFunction.

    Reduction is performed by matching the reduceNode in each series against
    the list of reduceMatchers. The each series is then passed to the
    reduceFunction as arguments in the order given by reduceMatchers. The
    reduceFunction should yield a single series.

    The resulting list of series are aliased so that they can easily be
    nested in other functions.

    **Example**: Map/Reduce asPercent(bytes_used,total_bytes) for each server.

    Assume that metrics in the form below exist::

        servers.server1.disk.bytes_used
        servers.server1.disk.total_bytes
        servers.server2.disk.bytes_used
        servers.server2.disk.total_bytes
        servers.server3.disk.bytes_used
        servers.server3.disk.total_bytes
        ...
        servers.serverN.disk.bytes_used
        servers.serverN.disk.total_bytes

    To get the percentage of disk used for each server::

        reduceSeries(mapSeries(servers.*.disk.*,1),
                     "asPercent",3,"bytes_used","total_bytes") =>

            alias(asPercent(servers.server1.disk.bytes_used,
                            servers.server1.disk.total_bytes),
                  "servers.server1.disk.reduce.asPercent"),
            alias(asPercent(servers.server2.disk.bytes_used,
                            servers.server2.disk.total_bytes),
                  "servers.server2.disk.reduce.asPercent"),
            ...
            alias(asPercent(servers.serverN.disk.bytes_used,
                            servers.serverN.disk.total_bytes),
                  "servers.serverN.disk.reduce.asPercent")

    In other words, we will get back the following metrics::

        servers.server1.disk.reduce.asPercent,
        servers.server2.disk.reduce.asPercent,
        ...
        servers.serverN.disk.reduce.asPercent

    .. seealso:: :py:func:`mapSeries`
    """
    metaSeries = {}
    keys = []
    for seriesList in seriesLists:
        for series in seriesList:
            nodes = series.name.split('.')
            node = nodes[reduceNode]
            reduceSeriesName = '.'.join(
                nodes[0:reduceNode]) + '.reduce.' + reduceFunction
            if node in reduceMatchers:
                if reduceSeriesName not in metaSeries:
                    metaSeries[reduceSeriesName] = [None] * len(reduceMatchers)
                    keys.append(reduceSeriesName)
                i = reduceMatchers.index(node)
                metaSeries[reduceSeriesName][i] = series
    for key in keys:
        metaSeries[key] = app.functions[reduceFunction](
            requestContext, *[[s] for s in metaSeries[key]])[0]
        metaSeries[key].name = key
    return [metaSeries[key] for key in keys]


def applyByNode(requestContext, seriesList, nodeNum, templateFunction,
                newName=None):
    """
    Takes a seriesList and applies some complicated function (described by
    a string), replacing templates with unique prefixes of keys from the
    seriesList (the key is all nodes up to the index given as `nodeNum`).

    If the `newName` parameter is provided, the name of the resulting series
    will be given by that parameter, with any "%" characters replaced by the
    unique prefix.

    Example::

        &target=applyByNode(servers.*.disk.bytes_free,1,
                "divideSeries(%.disk.bytes_free,sumSeries(%.disk.bytes_*))")

    Would find all series which match `servers.*.disk.bytes_free`, then trim
    them down to unique series up to the node given by nodeNum, then fill them
    into the template function provided (replacing % by the prefixes).

    """
    from .app import evaluateTarget
    prefixes = set()
    for series in seriesList:
        prefix = '.'.join(series.name.split('.')[:nodeNum + 1])
        prefixes.add(prefix)
    results = []
    for prefix in sorted(prefixes):
        target = templateFunction.replace('%', prefix)
        for resultSeries in evaluateTarget(requestContext, target):
            if newName:
                resultSeries.name = newName.replace('%', prefix)
            resultSeries.pathExpression = prefix
            resultSeries.start = series.start
            resultSeries.end = series.end
            results.append(resultSeries)
    return results


def groupByNode(requestContext, seriesList, nodeNum, callback):
    """
    Takes a serieslist and maps a callback to subgroups within as defined by a
    common node.

    Example::

        &target=groupByNode(ganglia.by-function.*.*.cpu.load5,2,"sumSeries")

    Would return multiple series which are each the result of applying the
    "sumSeries" function to groups joined on the second node (0 indexed)
    resulting in a list of targets like::

        sumSeries(ganglia.by-function.server1.*.cpu.load5),
        sumSeries(ganglia.by-function.server2.*.cpu.load5),...

    """
    return groupByNodes(requestContext, seriesList, callback, nodeNum)


def groupByNodes(requestContext, seriesList, callback, *nodes):
    """
    Takes a serieslist and maps a callback to subgroups within as defined by
    multiple nodes.

    Example::

        &target=groupByNodes(ganglia.server*.*.cpu.load*,"sumSeries",1,4)

    Would return multiple series which are each the result of applying the
    "sumSeries" function to groups joined on the nodes' list (0 indexed)
    resulting in a list of targets like::

        sumSeries(ganglia.server1.*.cpu.load5),
        sumSeries(ganglia.server1.*.cpu.load10),
        sumSeries(ganglia.server1.*.cpu.load15),
        sumSeries(ganglia.server2.*.cpu.load5),
        sumSeries(ganglia.server2.*.cpu.load10),
        sumSeries(ganglia.server2.*.cpu.load15), ...

    """
    from .app import app
    metaSeries = {}
    keys = []
    if isinstance(nodes, int):
        nodes = [nodes]
    for series in seriesList:
        key = '.'.join(series.name.split(".")[n] for n in nodes)
        if key not in metaSeries:
            metaSeries[key] = [series]
            keys.append(key)
        else:
            metaSeries[key].append(series)
    for key in metaSeries:
        metaSeries[key] = app.functions[callback](requestContext,
                                                  metaSeries[key])[0]
        metaSeries[key].name = key
    return [metaSeries[key] for key in keys]


def exclude(requestContext, seriesList, pattern):
    """
    Takes a metric or a wildcard seriesList, followed by a regular expression
    in double quotes.    Excludes metrics that match the regular expression.

    Example::

        &target=exclude(servers*.instance*.threads.busy,"server02")
    """
    regex = re.compile(pattern)
    return [s for s in seriesList if not regex.search(s.name)]


def grep(requestContext, seriesList, pattern):
    """
    Takes a metric or a wildcard seriesList, followed by a regular expression
    in double quotes. Excludes metrics that don't match the regular
    expression.

    Example::

        &target=grep(servers*.instance*.threads.busy,"server02")
    """
    regex = re.compile(pattern)
    return [s for s in seriesList if regex.search(s.name)]


def smartSummarize(requestContext, seriesList, intervalString, func='sum'):
    """
    Smarter experimental version of summarize.
    """
    results = []
    delta = parseTimeOffset(intervalString)
    interval = to_seconds(delta)

    # Adjust the start time to fit an entire day for intervals >= 1 day
    requestContext = requestContext.copy()
    tzinfo = requestContext['tzinfo']
    s = requestContext['startTime']
    if interval >= DAY:
        requestContext['startTime'] = datetime(s.year, s.month, s.day,
                                               tzinfo=tzinfo)
    elif interval >= HOUR:
        requestContext['startTime'] = datetime(s.year, s.month, s.day, s.hour,
                                               tzinfo=tzinfo)
    elif interval >= MINUTE:
        requestContext['startTime'] = datetime(s.year, s.month, s.day, s.hour,
                                               s.minute, tzinfo=tzinfo)

    paths = []
    for series in seriesList:
        paths.extend(pathsFromTarget(requestContext, series.pathExpression))
    data_store = fetchData(requestContext, paths)

    for series in seriesList:
        # XXX: breaks with summarize(metric.{a,b})
        #            each series.pathExpression == metric.{a,b}
        newSeries = evaluateTarget(requestContext,
                                   series.pathExpression,
                                   data_store)[0]
        series[0:len(series)] = newSeries
        series.start = newSeries.start
        series.end = newSeries.end
        series.step = newSeries.step

    for series in seriesList:
        buckets = {}  # {timestamp: [values]}

        timestamps = range(int(series.start), int(series.end),
                           int(series.step))
        datapoints = zip_longest(timestamps, series)

        # Populate buckets
        for timestamp, value in datapoints:
            # ISSUE: Sometimes there is a missing timestamp in datapoints when
            #        running a smartSummary
            if not timestamp:
                continue
            bucketInterval = int((timestamp - series.start) / interval)

            if bucketInterval not in buckets:
                buckets[bucketInterval] = []

            if value is not None:
                buckets[bucketInterval].append(value)

        newValues = []
        for timestamp in range(series.start, series.end, interval):
            bucketInterval = int((timestamp - series.start) / interval)
            bucket = buckets.get(bucketInterval, [])

            if bucket:
                if func == 'avg':
                    newValues.append(float(sum(bucket)) / float(len(bucket)))
                elif func == 'last':
                    newValues.append(bucket[len(bucket)-1])
                elif func == 'max':
                    newValues.append(max(bucket))
                elif func == 'min':
                    newValues.append(min(bucket))
                else:
                    newValues.append(sum(bucket))
            else:
                newValues.append(None)

        newName = "smartSummarize(%s, \"%s\", \"%s\")" % (series.name,
                                                          intervalString,
                                                          func)
        alignedEnd = series.start + (bucketInterval * interval) + interval
        newSeries = TimeSeries(newName, series.start, alignedEnd, interval,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)

    return results


def summarize(requestContext, seriesList, intervalString, func='sum',
              alignToFrom=False):
    """
    Summarize the data into interval buckets of a certain size.

    By default, the contents of each interval bucket are summed together.
    This is useful for counters where each increment represents a discrete
    event and retrieving a "per X" value requires summing all the events in
    that interval.

    Specifying 'avg' instead will return the mean for each bucket, which can
    be more useful when the value is a gauge that represents a certain value
    in time.

    'max', 'min' or 'last' can also be specified.

    By default, buckets are calculated by rounding to the nearest interval.
    This works well for intervals smaller than a day. For example, 22:32 will
    end up in the bucket 22:00-23:00 when the interval=1hour.

    Passing alignToFrom=true will instead create buckets starting at the from
    time. In this case, the bucket for 22:32 depends on the from time. If
    from=6:30 then the 1hour bucket for 22:32 is 22:30-23:30.

    Example::

        # total errors per hour
        &target=summarize(counter.errors, "1hour")

        # new users per week
        &target=summarize(nonNegativeDerivative(gauge.num_users), "1week")

        # average queue size per hour
        &target=summarize(queue.size, "1hour", "avg")

        # maximum queue size during each hour
        &target=summarize(queue.size, "1hour", "max")

        # 2010 Q1-4
        &target=summarize(metric, "13week", "avg", true)&from=midnight+20100101
    """
    results = []
    delta = parseTimeOffset(intervalString)
    interval = to_seconds(delta)

    for series in seriesList:
        buckets = {}

        timestamps = range(int(series.start), int(series.end) + 1,
                           int(series.step))
        datapoints = zip_longest(timestamps, series)

        for timestamp, value in datapoints:
            if timestamp is None:
                continue
            if alignToFrom:
                bucketInterval = int((timestamp - series.start) / interval)
            else:
                bucketInterval = timestamp - (timestamp % interval)

            if bucketInterval not in buckets:
                buckets[bucketInterval] = []

            if value is not None:
                buckets[bucketInterval].append(value)

        if alignToFrom:
            newStart = series.start
            newEnd = series.end
        else:
            newStart = series.start - (series.start % interval)
            newEnd = series.end - (series.end % interval) + interval

        newValues = []
        for timestamp in range(newStart, newEnd, interval):
            if alignToFrom:
                newEnd = timestamp
                bucketInterval = int((timestamp - series.start) / interval)
            else:
                bucketInterval = timestamp - (timestamp % interval)

            bucket = buckets.get(bucketInterval, [])

            if bucket:
                if func == 'avg':
                    newValues.append(float(sum(bucket)) / float(len(bucket)))
                elif func == 'last':
                    newValues.append(bucket[len(bucket)-1])
                elif func == 'max':
                    newValues.append(max(bucket))
                elif func == 'min':
                    newValues.append(min(bucket))
                else:
                    newValues.append(sum(bucket))
            else:
                newValues.append(None)

        if alignToFrom:
            newEnd += interval

        newName = "summarize(%s, \"%s\", \"%s\"%s)" % (
            series.name, intervalString, func, alignToFrom and ", true" or "")
        newSeries = TimeSeries(newName, newStart, newEnd, interval, newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)

    return results


def hitcount(requestContext, seriesList, intervalString,
             alignToInterval=False):
    """
    Estimate hit counts from a list of time series.

    This function assumes the values in each time series represent
    hits per second.    It calculates hits per some larger interval
    such as per day or per hour.    This function is like summarize(),
    except that it compensates automatically for different time scales
    (so that a similar graph results from using either fine-grained
    or coarse-grained records) and handles rarely-occurring events
    gracefully.
    """
    results = []
    delta = parseTimeOffset(intervalString)
    interval = to_seconds(delta)

    if alignToInterval:
        requestContext = requestContext.copy()
        tzinfo = requestContext['tzinfo']
        s = requestContext['startTime']
        if interval >= DAY:
            requestContext['startTime'] = datetime(s.year, s.month, s.day,
                                                   tzinfo=tzinfo)
        elif interval >= HOUR:
            requestContext['startTime'] = datetime(s.year, s.month, s.day,
                                                   s.hour, tzinfo=tzinfo)
        elif interval >= MINUTE:
            requestContext['startTime'] = datetime(s.year, s.month, s.day,
                                                   s.hour, s.minute,
                                                   tzinfo=tzinfo)

        # Gather all paths first, then the data
        paths = []
        for series in seriesList:
            paths.extend(pathsFromTarget(requestContext,
                                         series.pathExpression))
        data_store = fetchData(requestContext, paths)

        for series in seriesList:
            newSeries = evaluateTarget(requestContext,
                                       series.pathExpression,
                                       data_store)[0]
            intervalCount = int((series.end - series.start) / interval)
            series[0:len(series)] = newSeries
            series.start = newSeries.start
            series.end = newSeries.start + (
                intervalCount * interval) + interval
            series.step = newSeries.step

    for series in seriesList:
        step = int(series.step)
        bucket_count = int(math.ceil(
            float(series.end - series.start) / interval))
        buckets = [[] for _ in range(bucket_count)]
        newStart = int(series.end - bucket_count * interval)

        for i, value in enumerate(series):
            if value is None:
                continue

            start_time = int(series.start + i * step)
            start_bucket, start_mod = divmod(start_time - newStart, interval)
            end_time = start_time + step
            end_bucket, end_mod = divmod(end_time - newStart, interval)

            if end_bucket >= bucket_count:
                end_bucket = bucket_count - 1
                end_mod = interval

            if start_bucket == end_bucket:
                # All of the hits go to a single bucket.
                if start_bucket >= 0:
                    buckets[start_bucket].append(value * (end_mod - start_mod))

            else:
                # Spread the hits among 2 or more buckets.
                if start_bucket >= 0:
                    buckets[start_bucket].append(
                        value * (interval - start_mod))
                hits_per_bucket = value * interval
                for j in range(start_bucket + 1, end_bucket):
                    buckets[j].append(hits_per_bucket)
                if end_mod > 0:
                    buckets[end_bucket].append(value * end_mod)

        newValues = []
        for bucket in buckets:
            if bucket:
                newValues.append(sum(bucket))
            else:
                newValues.append(None)

        newName = 'hitcount(%s, "%s"%s)' % (series.name, intervalString,
                                            alignToInterval and ", true" or "")
        newSeries = TimeSeries(newName, newStart, series.end, interval,
                               newValues)
        newSeries.pathExpression = newName
        results.append(newSeries)

    return results


def timeFunction(requestContext, name, step=60):
    """
    Short Alias: time()

    Just returns the timestamp for each X value. T

    Example::

        &target=time("The.time.series")

    This would create a series named "The.time.series" that contains in Y
    the same value (in seconds) as X.

    A second argument can be provided as a step parameter (default is 60 secs)

    """
    start = int(epoch(requestContext["startTime"]))
    end = int(epoch(requestContext["endTime"]))
    delta = timedelta(seconds=step)
    when = requestContext["startTime"]
    values = []

    while when < requestContext["endTime"]:
        values.append(epoch(when))
        when += delta

    series = TimeSeries(name, start, end, step, values)
    series.pathExpression = name

    return [series]


def sinFunction(requestContext, name, amplitude=1, step=60):
    """
    Short Alias: sin()

    Just returns the sine of the current time. The optional amplitude parameter
    changes the amplitude of the wave.

    Example::

        &target=sin("The.time.series", 2)

    This would create a series named "The.time.series" that contains sin(x)*2.

    A third argument can be provided as a step parameter (default is 60 secs).
    """
    delta = timedelta(seconds=step)
    when = requestContext["startTime"]
    values = []

    while when < requestContext["endTime"]:
        values.append(math.sin(epoch(when))*amplitude)
        when += delta

    series = TimeSeries(
        name, int(epoch(requestContext["startTime"])),
        int(epoch(requestContext["endTime"])),
        step, values)
    series.pathExpression = 'sin({0})'.format(name)
    return [series]


def removeEmptySeries(requestContext, seriesList):
    """
    Takes one metric or a wildcard seriesList. Out of all metrics
    passed, draws only the metrics with not empty data.

    Example::

        &target=removeEmptySeries(server*.instance*.threads.busy)

    Draws only live servers with not empty data.
    """
    return [series for series in seriesList if not_empty(series)]


def randomWalkFunction(requestContext, name, step=60):
    """
    Short Alias: randomWalk()

    Returns a random walk starting at 0. This is great for testing when there
    is no real data in whisper.

    Example::

        &target=randomWalk("The.time.series")

    This would create a series named "The.time.series" that contains points
    where x(t) == x(t-1)+random()-0.5, and x(0) == 0.

    Accepts an optional second argument as step parameter (default step is
    60 sec).
    """
    delta = timedelta(seconds=step)
    when = requestContext["startTime"]
    values = []
    current = 0
    while when < requestContext["endTime"]:
        values.append(current)
        current += random.random() - 0.5
        when += delta

    return [TimeSeries(
        name, int(epoch(requestContext["startTime"])),
        int(epoch(requestContext["endTime"])),
        step, values)]


def pieAverage(requestContext, series):
    return safeAvg(series)


def pieMaximum(requestContext, series):
    return safeMax(series)


def pieMinimum(requestContext, series):
    return safeMin(series)


PieFunctions = {
    'average': pieAverage,
    'maximum': pieMaximum,
    'minimum': pieMinimum,
}

SeriesFunctions = {
    # Combine functions
    'sumSeries': sumSeries,
    'sum': sumSeries,
    'multiplySeries': multiplySeries,
    'averageSeries': averageSeries,
    'stddevSeries': stddevSeries,
    'avg': averageSeries,
    'sumSeriesWithWildcards': sumSeriesWithWildcards,
    'averageSeriesWithWildcards': averageSeriesWithWildcards,
    'multiplySeriesWithWildcards': multiplySeriesWithWildcards,
    'minSeries': minSeries,
    'maxSeries': maxSeries,
    'rangeOfSeries': rangeOfSeries,
    'percentileOfSeries': percentileOfSeries,
    'countSeries': countSeries,
    'weightedAverage': weightedAverage,

    # Transform functions
    'scale': scale,
    'scaleToSeconds': scaleToSeconds,
    'offset': offset,
    'offsetToZero': offsetToZero,
    'derivative': derivative,
    'delay': delay,
    'squareRoot': squareRoot,
    'pow': pow,
    'powSeries': powSeries,
    'perSecond': perSecond,
    'integral': integral,
    'integralByInterval': integralByInterval,
    'nonNegativeDerivative': nonNegativeDerivative,
    'log': logarithm,
    'invert': invert,
    'timeStack': timeStack,
    'timeShift': timeShift,
    'timeSlice': timeSlice,
    'summarize': summarize,
    'smartSummarize': smartSummarize,
    'hitcount': hitcount,
    'absolute': absolute,
    'interpolate': interpolate,

    # Calculate functions
    'movingAverage': movingAverage,
    'movingMedian': movingMedian,
    'movingSum': movingSum,
    'movingMin': movingMin,
    'movingMax': movingMax,
    'stdev': stdev,
    'holtWintersForecast': holtWintersForecast,
    'holtWintersConfidenceBands': holtWintersConfidenceBands,
    'holtWintersConfidenceArea': holtWintersConfidenceArea,
    'holtWintersAberration': holtWintersAberration,
    'linearRegression': linearRegression,
    'asPercent': asPercent,
    'pct': asPercent,
    'diffSeries': diffSeries,
    'divideSeriesLists': divideSeriesLists,
    'divideSeries': divideSeries,
    'exponentialMovingAverage': exponentialMovingAverage,

    # Series Filter functions
    'fallbackSeries': fallbackSeries,
    'mostDeviant': mostDeviant,
    'highestCurrent': highestCurrent,
    'lowestCurrent': lowestCurrent,
    'highestMax': highestMax,
    'currentAbove': currentAbove,
    'currentBelow': currentBelow,
    'highestAverage': highestAverage,
    'lowestAverage': lowestAverage,
    'averageAbove': averageAbove,
    'averageBelow': averageBelow,
    'maximumAbove': maximumAbove,
    'minimumAbove': minimumAbove,
    'maximumBelow': maximumBelow,
    'minimumBelow': minimumBelow,
    'nPercentile': nPercentile,
    'limit': limit,
    'sortByTotal': sortByTotal,
    'sortByName': sortByName,
    'averageOutsidePercentile': averageOutsidePercentile,
    'removeBetweenPercentile': removeBetweenPercentile,
    'sortByMaxima': sortByMaxima,
    'sortByMinima': sortByMinima,
    'useSeriesAbove': useSeriesAbove,
    'exclude': exclude,
    'grep': grep,
    'removeEmptySeries': removeEmptySeries,

    # Data Filter functions
    'removeAbovePercentile': removeAbovePercentile,
    'removeAboveValue': removeAboveValue,
    'removeBelowPercentile': removeBelowPercentile,
    'removeBelowValue': removeBelowValue,

    # Special functions
    'legendValue': legendValue,
    'alias': alias,
    'aliasSub': aliasSub,
    'aliasByNode': aliasByNode,
    'aliasByMetric': aliasByMetric,
    'cactiStyle': cactiStyle,
    'color': color,
    'alpha': alpha,
    'cumulative': cumulative,
    'consolidateBy': consolidateBy,
    'keepLastValue': keepLastValue,
    'changed': changed,
    'drawAsInfinite': drawAsInfinite,
    'secondYAxis': secondYAxis,
    'lineWidth': lineWidth,
    'dashed': dashed,
    'substr': substr,
    'group': group,
    'map': mapSeries,
    'mapSeries': mapSeries,
    'reduce': reduceSeries,
    'reduceSeries': reduceSeries,
    'applyByNode': applyByNode,
    'groupByNode': groupByNode,
    'groupByNodes': groupByNodes,
    'constantLine': constantLine,
    'stacked': stacked,
    'areaBetween': areaBetween,
    'verticalLine': verticalLine,
    'threshold': threshold,
    'transformNull': transformNull,
    'isNonNull': isNonNull,
    'identity': identity,
    'aggregateLine': aggregateLine,

    # test functions
    'time': timeFunction,
    "sin": sinFunction,
    "randomWalk": randomWalkFunction,
    'timeFunction': timeFunction,
    "sinFunction": sinFunction,
    "randomWalkFunction": randomWalkFunction,
}

from .app import app  # noqa
from .evaluator import evaluateTarget, evaluateTokens, pathsFromTarget  # noqa
