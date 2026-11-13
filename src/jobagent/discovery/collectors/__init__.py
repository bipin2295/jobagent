"""Job-board collectors – one module per ATS / source.

Import all concrete collectors here so the rest of the codebase can do::

    from jobagent.discovery.collectors import GreenhouseCollector, ...
"""

from jobagent.discovery.collectors.arbeitnow import ArbeitnowCollector
from jobagent.discovery.collectors.ashby import AshbyCollector
from jobagent.discovery.collectors.greenhouse import GreenhouseCollector
from jobagent.discovery.collectors.himalayas import HimalayasCollector
from jobagent.discovery.collectors.lever import LeverCollector
from jobagent.discovery.collectors.remoteok import RemoteOKCollector
from jobagent.discovery.collectors.remotive import RemotiveCollector
from jobagent.discovery.collectors.smartrecruiters import SmartRecruitersCollector
from jobagent.discovery.collectors.weworkremotely import WeWorkRemotelyCollector
from jobagent.discovery.collectors.workable import WorkableCollector
from jobagent.discovery.collectors.workday import WorkdayCollector

__all__ = [
    "ArbeitnowCollector",
    "AshbyCollector",
    "GreenhouseCollector",
    "HimalayasCollector",
    "LeverCollector",
    "RemoteOKCollector",
    "RemotiveCollector",
    "SmartRecruitersCollector",
    "WeWorkRemotelyCollector",
    "WorkableCollector",
    "WorkdayCollector",
]
