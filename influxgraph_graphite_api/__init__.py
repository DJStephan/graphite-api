DEBUG = False

from ._version import get_versions # flake8: noqa
__version__ = get_versions()['version']
del get_versions
