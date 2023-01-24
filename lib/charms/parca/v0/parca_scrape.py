# Copyright 2022 Jon Seager
# See LICENSE file for licensing details.
"""## Overview.

This document explains how to integrate with the Parca charm for the purpose of providing a
profiling endpoint to Parca. It also explains how alternative implementations of the Parca charms
may maintain the same interface and be backward compatible with all currently integrated charms.

## Provider Library Usage

This Parca charm interacts with its scrape targets using its charm library. Charms seeking to
expose profiling endpoints for the Parca charm, must do so using the `ProfilingEndpointProvider`
object from this charm library. For the simplest use cases, using the `ProfilingEndpointProvider`
object only requires instantiating it, typically in the constructor of your charm. The
`ProfilingEndpointProvider` constructor requires the name of the relation over which a scrape
target (profiling endpoint) is exposed to the Parca charm. This relation must use the
`parca_scrape` interface. By default address of the profiling endpoint is set to the unit IP
address, by each unit of the `ProfilingEndpointProvider` charm. These units set their address in
response to the `PebbleReady` event of each container in the unit, since container restarts of
Kubernetes charms can result in change of IP addresses. The default name for the profiling endpoint
relation is `profiling-endpoint`. It is strongly recommended to use the same relation name for
consistency across charms and doing so obviates the need for an additional constructor argument.
The `ProfilingEndpointProvider` object may be instantiated as follows

    from charms.parca.v0.parca_scrape import ProfilingEndpointProvider

    def __init__(self, *args):
        super().__init__(*args)
        # ...
        self.profiling_endpoint = ProfilingEndpointProvider(self)
        # ...

Note that the first argument (`self`) to `ProfilingEndpointProvider` is always a reference to the
parent (scrape target) charm.

An instantiated `ProfilingEndpointProvider` object will ensure that each unit of its parent charm,
is a scrape target for the `ProfilingEndpointConsumer` (Parca) charm. By default
`ProfilingEndpointProvider` assumes each unit of the consumer charm exports its profiles on port
80. These defaults may be changed by providing the `ProfilingEndpointProvider` constructor an
optional argument (`jobs`) that represents a Parca scrape job specification using Python standard
data structures. This job specification is a subset of Parca's own [scrape
configuration](https://www.parca.dev/docs/configuration) format but represented using Python data
structures. More than one job may be provided using the `jobs` argument. Hence `jobs` accepts a
list of dictionaries where each dictionary represents one `<scrape_config>` object as described in
the Parca documentation. The currently supported configuration subset is: `job_name`,
`static_configs`

Suppose it is required to change the port on which scraped profiles are exposed to 8000. This may be
done by providing the following data structure as the value of `jobs`.

```python
[{"static_configs": [{"targets": ["*:8000"]}]}]
```

The wildcard ("*") host specification implies that the scrape targets will automatically be set to
the host addresses advertised by each unit of the consumer charm.

It is also possible to change the profile path and scrape multiple ports, for example

```
[{"static_configs": [{"targets": ["*:8000", "*:8081"]}]}]
```

More complex scrape configurations are possible. For example

```
[{
    "static_configs": [{
        "targets": ["10.1.32.215:7000", "*:8000"],
        "labels": {
            "some-key": "some-value"
        }
    }]
}]
```

This example scrapes the target "10.1.32.215" at port 7000 in addition to scraping each unit at
port 8000. There is however one difference between wildcard targets (specified using "*") and fully
qualified targets (such as "10.1.32.215"). The Parca charm automatically associates labels with
profiles generated by each target. These labels localise the source of profiles within the Juju
topology by specifying its "model name", "model UUID", "application name" and "unit name". However
unit name is associated only with wildcard targets but not with fully qualified targets.

Multiple jobs with labels are allowed, but each job must be given a unique name:

```
[
    {
        "job_name": "my-first-job",
        "static_configs": [
            {
                "targets": ["*:7000"],
                "labels": {
                    "some-key": "some-value"
                }
            }
        ]
    },
    {
        "job_name": "my-second-job",
        "static_configs": [
            {
                "targets": ["*:8000"],
                "labels": {
                    "some-other-key": "some-other-value"
                }
            }
        ]
    }
]
```

**Important:** `job_name` should be a fixed string (e.g. hardcoded literal). For instance, if you
include variable elements, like your `unit.name`, it may break the continuity of the profile time
series gathered by Parca when the leader unit changes (e.g. on upgrade or rescale).

## Consumer Library Usage

The `ProfilingEndpointConsumer` object may be used by Parca charms to manage relations with their
scrape targets. For this purposes a Parca charm needs to do two things

1. Instantiate the `ProfilingEndpointConsumer` object by providing it a
reference to the parent (Parca) charm and optionally the name of the relation that the Parca charm
uses to interact with scrape targets. This relation must confirm to the `parca_scrape` interface
and it is strongly recommended that this relation be named `profiling-endpoint` which is its
default value.

For example a Parca charm may instantiate the `ProfilingEndpointConsumer` in its constructor as
follows

    from charms.parca.v0.parca_scrape import ProfilingEndpointConsumer

    def __init__(self, *args):
        super().__init__(*args)
        # ...
        self.profiling_consumer = ProfilingEndpointConsumer(self)
        # ...

2. A Parca charm also needs to respond to the `TargetsChangedEvent` event of the
`ProfilingEndpointConsumer` by adding itself as an observer for these events, as in

    self.framework.observe(
        self.profiling_consumer.on.targets_changed,
        self._on_scrape_targets_changed,
    )

In responding to the `TargetsChangedEvent` event the Parca charm must update the Parca
configuration so that any new scrape targets are added and/or old ones removed from the list of
scraped endpoints. For this purpose the `ProfilingEndpointConsumer` object exposes a `jobs()`
method that returns a list of scrape jobs. Each element of this list is the Parca scrape
configuration for that job. In order to update the Parca configuration, the Parca charm needs to
replace the current list of jobs with the list provided by `jobs()` as follows

    def _on_scrape_targets_changed(self, event):
        ...
        scrape_jobs = self.profiling_consumer.jobs()
        for job in scrape_jobs:
            parca_scrape_config.append(job)
        ...

## Relation Data

Units of profiles provider charms advertise their names and addresses over unit relation data using
the `parca_scrape_unit_name` and `parca_scrape_unit_address` keys. While the `scrape_metadata`,
`scrape_jobs` and `alert_rules` keys in application relation data of profiles provider charms hold
eponymous information.

"""  # noqa: W505

import ipaddress
import json
import logging
import socket
from typing import List, Optional, Union

from charms.observability_libs.v0.juju_topology import JujuTopology
from ops.charm import CharmBase, RelationRole
from ops.framework import BoundEvent, EventBase, EventSource, Object, ObjectEvents

# The unique Charmhub library identifier, never change it
LIBID = "7b30b495435746acb645ca414898621f"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 4

logger = logging.getLogger(__name__)


ALLOWED_KEYS = {"job_name", "static_configs", "scrape_interval", "scrape_timeout"}
DEFAULT_JOB = {"static_configs": [{"targets": ["*:80"]}]}
DEFAULT_RELATION_NAME = "profiling-endpoint"
RELATION_INTERFACE_NAME = "parca_scrape"


class RelationNotFoundError(Exception):
    """Raise if there is no relation with the given name is found."""

    def __init__(self, relation_name: str):
        self.relation_name = relation_name
        self.message = "No relation named '{}' found".format(relation_name)
        super().__init__(self.message)


class RelationInterfaceMismatchError(Exception):
    """Raise if the relation with the given name has a different interface."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_interface: str,
        actual_relation_interface: str,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_interface
        self.actual_relation_interface = actual_relation_interface
        self.message = (
            "The '{}' relation has '{}' as interface rather than the expected '{}'".format(
                relation_name, actual_relation_interface, expected_relation_interface
            )
        )

        super().__init__(self.message)


class RelationRoleMismatchError(Exception):
    """Raise if the relation with the given name has a different role."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_role: RelationRole,
        actual_relation_role: RelationRole,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_role
        self.actual_relation_role = actual_relation_role
        self.message = "The '{}' relation has role '{}' rather than the expected '{}'".format(
            relation_name, repr(actual_relation_role), repr(expected_relation_role)
        )

        super().__init__(self.message)


def _validate_relation_by_interface_and_direction(
    charm: CharmBase,
    relation_name: str,
    expected_relation_interface: str,
    expected_relation_role: RelationRole,
):
    """Verify that a relation has the necessary characteristics.

    Verifies that the `relation_name` provided: (1) exists in metadata.yaml,
    (2) declares as interface the interface name passed as `relation_interface`
    and (3) has the right "direction", i.e., it is a relation that `charm`
    provides or requires.

    Args:
        charm: a `CharmBase` object to scan for the matching relation.
        relation_name: the name of the relation to be verified.
        expected_relation_interface: the interface name to be matched by the
            relation named `relation_name`.
        expected_relation_role: whether the `relation_name` must be either
            provided or required by `charm`.

    Raises:
        RelationNotFoundError: If there is no relation in the charm's metadata.yaml
            with the same name as provided via `relation_name` argument.
        RelationInterfaceMismatchError: The relation with the same name as provided
            via `relation_name` argument does not have the same relation interface
            as specified via the `expected_relation_interface` argument.
        RelationRoleMismatchError: If the relation with the same name as provided
            via `relation_name` argument does not have the same role as specified
            via the `expected_relation_role` argument.
    """
    if relation_name not in charm.meta.relations:
        raise RelationNotFoundError(relation_name)

    relation = charm.meta.relations[relation_name]

    actual_relation_interface = relation.interface_name
    if actual_relation_interface != expected_relation_interface:
        raise RelationInterfaceMismatchError(
            relation_name, expected_relation_interface, actual_relation_interface
        )

    if expected_relation_role == RelationRole.provides:
        if relation_name not in charm.meta.provides:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.provides, RelationRole.requires
            )
    elif expected_relation_role == RelationRole.requires:
        if relation_name not in charm.meta.requires:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.requires, RelationRole.provides
            )
    else:
        raise Exception("Unexpected RelationDirection: {}".format(expected_relation_role))


def _sanitize_scrape_configuration(job) -> dict:
    """Restrict permissible scrape configuration options.

    If job is empty then a default job is returned. The default job is:

    ```python
    {"static_configs": [{"targets": ["*:80"]}]}
    ```

    Args:
        job: a dict containing a single Parca job specification.

    Returns:
        a dictionary containing a sanitized job specification.
    """
    sanitized_job = DEFAULT_JOB.copy()
    sanitized_job.update({key: value for key, value in job.items() if key in ALLOWED_KEYS})
    return sanitized_job


class ProviderTopology(JujuTopology):
    """Class for initializing topology information for ProfilingEndpointProvider."""

    @property
    def scrape_identifier(self):
        """Format the topology information into a scrape identifier."""
        # This is used only by Profiling[Consumer|Provider] and does not need a
        # unit name, so only check for the charm name
        return "juju_{}_parca_scrape".format(self.identifier)


class TargetsChangedEvent(EventBase):
    """Event emitted when Parca scrape targets change."""

    def __init__(self, handle, relation_id):
        super().__init__(handle)
        self.relation_id = relation_id

    def snapshot(self):
        """Save scrape target relation information."""
        return {"relation_id": self.relation_id}

    def restore(self, snapshot):
        """Restore scrape target relation information."""
        self.relation_id = snapshot["relation_id"]


class MonitoringEvents(ObjectEvents):
    """Event descriptor for events raised by `ProfilingEndpointConsumer`."""

    targets_changed = EventSource(TargetsChangedEvent)


class ProfilingEndpointConsumer(Object):
    """Parca based monitoring service."""

    on = MonitoringEvents()

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Construct a Parca based monitoring service.

        Args:
            charm: a `CharmBase` instance that manages this instance of the Parca service.
            relation_name: an optional string name of the relation between `charm`
                and the Parca charmed service. The default is "profiling-endpoint".

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `parca_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.requires`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.requires
        )

        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        events = self._charm.on[relation_name]
        self.framework.observe(
            events.relation_changed, self.on_profiling_provider_relation_changed
        )
        self.framework.observe(
            events.relation_departed, self._on_profiling_provider_relation_departed
        )

    def on_profiling_provider_relation_changed(self, event):
        """Handle changes with related profiling providers.

        Anytime there are changes in relations between Parca and profiling provider charms the
        Parca charm is informed, through a `TargetsChangedEvent` event. The Parca charm can then
        choose to update its scrape configuration.

        Args:
            event: a `CharmEvent` resulting in the Parca charm updating its scrape configuration
        """
        rel_id = event.relation.id

        self.on.targets_changed.emit(relation_id=rel_id)

    def _on_profiling_provider_relation_departed(self, event):
        """Update job config when a profiling provider departs.

        When a profiling provider departs the Parca charm is informed through a
        `TargetsChangedEvent` event so that it can update its scrape configuration to ensure that
        the departed profiling provider is removed from the list of scrape jobs.

        Args:
            event: a `CharmEvent` that indicates a profiling provider unit has departed.
        """
        rel_id = event.relation.id
        self.on.targets_changed.emit(relation_id=rel_id)

    def jobs(self) -> list:
        """Fetch the list of scrape jobs.

        Returns:
            A list consisting of all the static scrape configurations for each related
            `ProfilingEndpointProvider` that has specified its scrape targets.
        """
        scrape_jobs = []

        for relation in self._charm.model.relations[self._relation_name]:
            static_scrape_jobs = self._static_scrape_config(relation)
            if static_scrape_jobs:
                scrape_jobs.extend(static_scrape_jobs)

        return scrape_jobs

    def _static_scrape_config(self, relation) -> list:
        """Generate the static scrape configuration for a single relation.

        If the relation data includes `scrape_metadata` then the value of this key is used to
        annotate the scrape jobs with Juju Topology labels before returning them.

        Args:
            relation: an `ops.model.Relation` object whose static scrape configuration is required.

        Returns:
            A list (possibly empty) of scrape jobs. Each job is a valid Parca scrape configuration
            for that job, represented as a Python dictionary.
        """
        if not relation.units:
            return []

        scrape_jobs = json.loads(relation.data[relation.app].get("scrape_jobs", "[]"))

        if not scrape_jobs:
            return []

        scrape_metadata = json.loads(relation.data[relation.app].get("scrape_metadata", "{}"))

        if not scrape_metadata:
            return scrape_jobs

        job_name_prefix = JujuTopology.from_dict(scrape_metadata).identifier

        hosts = self._relation_hosts(relation)

        labeled_job_configs = []
        for job in scrape_jobs:
            config = self._labeled_static_job_config(
                _sanitize_scrape_configuration(job),
                job_name_prefix,
                hosts,
                scrape_metadata,
            )
            labeled_job_configs.append(config)

        return labeled_job_configs

    def _relation_hosts(self, relation) -> dict:
        """Fetch unit names and address of all profiling provider units for a single relation.

        Args:
            relation: An `ops.model.Relation` object for which the unit name to
                address mapping is required.

        Returns:
            A dictionary that maps unit names to unit addresses for the specified relation.
        """
        hosts = {}
        for unit in relation.units:
            # TODO deprecate and remove unit.name
            unit_name = relation.data[unit].get("parca_scrape_unit_name") or unit.name
            # TODO deprecate and remove "parca_scrape_host"
            unit_address = relation.data[unit].get("parca_scrape_unit_address") or relation.data[
                unit
            ].get("parca_scrape_host")
            if unit_name and unit_address:
                hosts.update({unit_name: unit_address})

        return hosts

    def _labeled_static_job_config(self, job, job_name_prefix, hosts, scrape_metadata) -> dict:
        """Construct labeled job configuration for a single job.

        Args:

            job: a dictionary representing the job configuration as obtained from
                `ProfilingEndpointProvider` over relation data.
            job_name_prefix: a string that may either be used as the
                job name if the job has no associated name or used as a prefix for
                the job if it does have a job name.
            hosts: a dictionary mapping host names to host address for
                all units of the relation for which this job configuration must be constructed.
            scrape_metadata: scrape configuration metadata obtained
                from `ProfilingEndpointProvider` from the same relation for
                which this job configuration is being constructed.

        Returns:
            A dictionary representing a Parca job configuration for a single job.
        """
        name = job.get("job_name")
        job_name = "{}_{}".format(job_name_prefix, name) if name else job_name_prefix

        labeled_job = job.copy()
        labeled_job["job_name"] = job_name

        static_configs = job.get("static_configs")
        labeled_job["static_configs"] = []

        # relabel instance labels so that instance identifiers are globally unique
        # stable over unit recreation
        instance_relabel_config = {
            "source_labels": ["juju_model", "juju_model_uuid", "juju_application"],
            "separator": "_",
            "target_label": "instance",
            "regex": "(.*)",
        }

        # label all static configs in the Parca job labeling inserts Juju topology information and
        # sets a relable config for instance labels
        for static_config in static_configs:
            labels = static_config.get("labels", {}) if static_configs else {}
            all_targets = static_config.get("targets", [])

            # split all targets into those which will have unit labels and those which will not
            ports = []
            unitless_targets = []
            for target in all_targets:
                host, port = target.split(":")
                if host.strip() == "*":
                    ports.append(port.strip())
                else:
                    unitless_targets.append(target)

            # label scrape targets that do not have unit labels
            if unitless_targets:
                unitless_config = self._labeled_unitless_config(
                    unitless_targets, labels, scrape_metadata
                )
                labeled_job["static_configs"].append(unitless_config)

            # label scrape targets that do have unit labels
            for host_name, host_address in hosts.items():
                static_config = self._labeled_unit_config(
                    host_name, host_address, ports, labels, scrape_metadata
                )
                labeled_job["static_configs"].append(static_config)
                if "juju_unit" not in instance_relabel_config["source_labels"]:
                    instance_relabel_config["source_labels"].append("juju_unit")  # type: ignore

        # ensure topology relabeling of instance label is last in order of relabelings
        relabel_configs = job.get("relabel_configs", [])
        relabel_configs.append(instance_relabel_config)
        labeled_job["relabel_configs"] = relabel_configs
        return labeled_job

    def _set_juju_labels(self, labels, scrape_metadata) -> dict:
        """Create a copy of metric labels with Juju topology information.

        Args:
            labels: a dictionary containing Parca metric labels.
            scrape_metadata: scrape related metadata provided by `ProfilingEndpointProvider`.

        Returns:
            a copy of the `labels` dictionary augmented with Juju topology information with the
            exception of unit name.
        """
        juju_labels = labels.copy()  # deep copy not needed
        juju_labels.update(ProviderTopology.from_dict(scrape_metadata).label_matcher_dict)

        return juju_labels

    def _labeled_unitless_config(self, targets, labels, scrape_metadata) -> dict:
        """Return static scrape configuration for fully qualified host addresses.

        Fully qualified hosts are those scrape targets for which the address are specified by the
        `ProfilingEndpointProvider` as part of the scrape job specification set in application
        relation data. The address specified need not belong to any unit of the
        `ProfilingEndpointProvider` charm. As a result there is no reliable way to determine the
        name (Juju topology unit name) for such a target.

        Args:
            targets: a list of addresses of fully qualified hosts.
            labels: labels specified by `ProfilingEndpointProvider` clients which are associated
                with `targets`.
            scrape_metadata: scrape related metadata provided by `ProfilingEndpointProvider`.

        Returns:
            A dict containing the static scrape configuration for a list of fully qualified hosts.
        """
        juju_labels = self._set_juju_labels(labels, scrape_metadata)
        unitless_config = {"targets": targets, "labels": juju_labels}
        return unitless_config

    def _labeled_unit_config(
        self, unit_name, host_address, ports, labels, scrape_metadata
    ) -> dict:
        """Return static scrape configuration for a wildcard host.

        Wildcard hosts are those scrape targets whose name (Juju unit name) and address (unit IP
        address) is set into unit relation data by the `ProfilingEndpointProvider` charm, which
        sets this data for ALL its units.

        Args:
            unit_name: a string representing the unit name of the wildcard host.
            host_address: a string representing the address of the wildcard host.
            ports: list of ports on which this wildcard host exposes its profiles.
            labels: a dictionary of labels provided by `ProfilingEndpointProvider` intended to be
                associated with this wildcard host.
            scrape_metadata: scrape related metadata provided by `ProfilingEndpointProvider`.

        Returns:
            A dictionary containing the static scrape configuration
            for a single wildcard host.
        """
        juju_labels = self._set_juju_labels(labels, scrape_metadata)

        juju_labels["juju_unit"] = unit_name

        static_config = {"labels": juju_labels}

        if ports:
            targets = []
            for port in ports:
                targets.append("{}:{}".format(host_address, port))
            static_config["targets"] = targets  # type: ignore
        else:
            static_config["targets"] = [host_address]  # type: ignore

        return static_config


class ProfilingEndpointProvider(Object):
    """Profiling endpoint for Parca."""

    def __init__(
        self,
        charm,
        relation_name: str = DEFAULT_RELATION_NAME,
        jobs=None,
        refresh_event: Optional[Union[BoundEvent, List[BoundEvent]]] = None,
    ):
        """Construct a profiling provider for a Parca charm.

        If your charm exposes a Parca profiling endpoint, the `ProfilingEndpointProvider` object
        enables your charm to easily communicate how to reach that endpoint.

        By default, a charm instantiating this object has the profiling endpoints of each of its
        units scraped by the related Parca charms.

        The scraped profiles are automatically tagged by the Parca charms with Juju topology data
        via the `juju_model_name`, `juju_model_uuid`, `juju_application_name` and `juju_unit`
        labels. To support such tagging `ProfilingEndpointProvider` automatically forwards scrape
        metadata to a `ProfilingEndpointConsumer` (Parca charm).

        Scrape targets provided by `ProfilingEndpointProvider` can be customized when instantiating
        this object. For example in the case of a charm exposing the profiling endpoint for each of
        its units on port 8080, the `ProfilingEndpointProvider` can be
        instantiated as follows:

            self.profiling_endpoint_provider = ProfilingEndpointProvider(
                self, jobs=[{"static_configs": [{"targets": ["*:8080"]}]}]
            )

        The notation `*:<port>` means "scrape each unit of this charm on port `<port>`.

        Args:
            charm: a `CharmBase` object that manages this
                `ProfilingEndpointProvider` object. Typically this is `self` in the instantiating
                class.
            relation_name: an optional string name of the relation between `charm`
                and the Parca charmed service. The default is "profiling-endpoint". It is strongly
                advised not to change the default, so that people deploying your charm will have a
                consistent experience with all other charms that provide profiling endpoints.
            jobs: an optional list of dictionaries where each dictionary represents the Parca
                scrape configuration for a single job. When not provided, a default scrape
                configuration is provided polling all units of the charm on port `80` using the
                `ProfilingEndpointProvider` object.
            refresh_event: an optional bound event or list of bound events which
                will be observed to re-set scrape job data (IP address and others)

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `parca_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.provides`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.provides
        )

        super().__init__(charm, relation_name)
        self.topology = ProviderTopology.from_charm(charm)

        self._charm = charm
        self._relation_name = relation_name
        # sanitize job configurations to the supported subset of parameters
        jobs = [] if jobs is None else jobs
        self._jobs = [_sanitize_scrape_configuration(job) for job in jobs]

        events = self._charm.on[self._relation_name]
        self.framework.observe(events.relation_joined, self._set_scrape_job_spec)
        self.framework.observe(events.relation_changed, self._set_scrape_job_spec)

        if not refresh_event:
            if len(self._charm.meta.containers) == 1:
                if "kubernetes" in self._charm.meta.series:
                    # This is a podspec charm
                    refresh_event = [self._charm.on.update_status]
                else:
                    # This is a sidecar/pebble charm
                    container = list(self._charm.meta.containers.values())[0]
                    refresh_event = [self._charm.on[container.name.replace("-", "_")].pebble_ready]
            else:
                refresh_event = [self._charm.on.update_status]

        else:
            if not isinstance(refresh_event, list):
                refresh_event = [refresh_event]

        for ev in refresh_event:
            self.framework.observe(ev, self._set_unit_ip)

        self.framework.observe(self._charm.on.upgrade_charm, self._set_scrape_job_spec)
        # If there is no leader during relation_joined we will still need to set alert rules.
        self.framework.observe(self._charm.on.leader_elected, self._set_scrape_job_spec)

    def _set_scrape_job_spec(self, event):
        """Ensure scrape target information is made available to Parca.

        When a profiling provider charm is related to a Parca charm, the profiling provider sets
        specification and metadata related to its own scrape configuration. This information is set
        using Juju application data. Each of the consumer units also sets its own host address in
        Juju unit relation data.
        """
        self._set_unit_ip(event)

        if not self._charm.unit.is_leader():
            return

        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.app]["scrape_metadata"] = json.dumps(self._scrape_metadata)
            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(self._scrape_jobs)

    def _set_unit_ip(self, _):
        """Set unit host address.

        Each time a profiling provider charm container is restarted it updates its own host address
        in the unit relation data for the Parca charm. The only argument specified is an event and
        it is ignored.
        """
        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.unit]["parca_scrape_unit_address"] = socket.getfqdn()
            relation.data[self._charm.unit]["parca_scrape_unit_name"] = str(
                self._charm.model.unit.name
            )

    def _is_valid_unit_address(self, address: str) -> bool:
        """Validate a unit address.

        Args:
            address: a string representing a unit address
        """
        try:
            _ = ipaddress.ip_address(address)
            return True
        except ValueError:
            return False

    @property
    def _scrape_jobs(self) -> list:
        """Fetch list of scrape jobs.

        Returns:
           A list of dictionaries, where each dictionary specifies a single scrape job for Parca.
        """
        return self._jobs if self._jobs else [DEFAULT_JOB]

    @property
    def _scrape_metadata(self) -> dict:
        """Generate scrape metadata.

        Returns:
            Scrape configuration metadata for this profiling provider charm.
        """
        return self.topology.as_dict()
