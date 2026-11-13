"""Discovery package – collectors, normaliser, deduplication.

Public surface::

    from jobagent.discovery import CollectorBase, CompanyUniverse, RawJob
    from jobagent.discovery.collectors import GreenhouseCollector, ...
"""

from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.discovery.company_universe import CompanyUniverse

__all__ = ["CollectorBase", "CompanyUniverse", "RawJob"]
