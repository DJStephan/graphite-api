#! /usr/bin/env bash
set -xe

if [ $TOXENV == "coverage" ]
then
	pip install -r requirements-dev.txt coverage codecov
	coverage run unittest_main.py
	codecov
else
	tox -e $TOXENV
fi
