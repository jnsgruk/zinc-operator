from socket import getfqdn
from unittest.mock import patch

import pytest
from charm import ZincCharm
from ops import pebble
from scenario import Container, Model, Relation, State


@pytest.fixture(autouse=True, scope="function")
def setup():
    with patch("charm.KubernetesServicePatch"):
        with patch.object(ZincCharm, "_request_version", lambda *_: "0.0-test"):
            yield


def test_nothing_happens_on_start():
    state = State()
    out = state.trigger("start", ZincCharm)

    # ignore juju log and storedstate changes
    out.juju_log = []
    out.stored_state = state.stored_state
    assert out.jsonpatch_delta(state) == []


def test_pebble_ready_admin_password():
    container = Container(name="zinc", can_connect=True)
    state = State(containers=[container])
    out = state.trigger(container.pebble_ready_event, ZincCharm)
    assert out.status.unit == ("active", "")
    env = out.get_container("zinc").layers["zinc"].services["zinc"].environment
    stored_state = next(filter(lambda sst: sst.owner_path == "ZincCharm", out.stored_state))
    assert env["ZINC_FIRST_ADMIN_PASSWORD"] == stored_state.content["initial_admin_password"]


def test_update_status():
    state = State(
        containers=[
            Container(
                name="zinc",
                can_connect=True,
                layers={"zinc": pebble.Layer({"services": {"zinc": {"startup": "enabled"}}})},
            )
        ]
    )
    out = state.trigger("update_status", ZincCharm)
    assert out.status.app_version == "0.0-test"


def test_ingress_integration():
    relation = Relation("ingress", interface="ingress")
    state = State(model=Model(name="mymodel"), leader=True, relations=[relation], app_name="myapp")
    out = state.trigger(relation.created_event, ZincCharm)
    lappdata = out.relations[0].local_app_data
    assert lappdata["model"] == "mymodel"
    assert lappdata["name"] == "myapp"
    assert lappdata["host"] == getfqdn()
    assert lappdata["port"] == "4080"  # default
