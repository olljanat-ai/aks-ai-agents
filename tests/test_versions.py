import datetime as dt

from aks_agents.versions import plan, speed_of

TODAY = dt.date(2026, 10, 10)
CATALOG = {
    "minors": {
        "1.32": {"ga": "2025-02-01", "end_of_support": "2026-11-15"},
        "1.33": {"ga": "2025-06-01", "end_of_support": "2027-03-01"},
        "1.34": {"ga": "2025-10-01", "end_of_support": "2027-07-01", "lts_end": "2027-03-01"},
        "1.35": {"ga": "2026-02-01", "end_of_support": "2027-11-01", "lts_end": "2028-02-01"},
    },
    "patches": {
        "1.32.9": "2026-05-01",
        "1.33.6": "2026-08-01",
        "1.33.7": "2026-09-20",
        "1.34.1": "2025-11-01",
        "1.34.4": "2026-09-25",
        "1.34.5": "2026-10-06",
        "1.35.2": "2026-06-01",
    },
    "node_images": {"AzureLinux3-202509.15.0": "2026-09-15", "AzureLinux3-202510.01.0": "2026-10-01"},
}


def step(steps, cluster, kind="kubernetes"):
    return next(s for s in steps if s.cluster == cluster and s.kind == kind)


def test_speeds():
    assert [speed_of(c) for c in ("aks-prd-sl-az1", "aks-prd-sl-az2", "aks-prd-sl-az3", "aks-prd-sf")] == [
        "fast",
        "slow",
        "slow",
        "stateful",
    ]


def test_fast_patch_one_week_per_environment_and_previous_env_gate():
    clusters = [
        {"environment": "dev", "name": "aks-dev-sl-az1", "kubernetes": "1.34.1"},
        {"environment": "acc", "name": "aks-acc-sl-az1", "kubernetes": "1.34.1"},
    ]
    steps = plan(CATALOG, clusters, TODAY)
    dev, acc = step(steps, "aks-dev-sl-az1"), step(steps, "aks-acc-sl-az1")
    assert (dev.action, dev.target) == ("upgrade", "1.34.4")  # 1.34.5 is only 4 days old
    assert acc.action == "hold" and "previous environment" in acc.reason


def test_acc_capped_to_what_dev_runs():
    clusters = [
        {"environment": "dev", "name": "aks-dev-sl-az1", "kubernetes": "1.34.4"},
        {"environment": "acc", "name": "aks-acc-sl-az1", "kubernetes": "1.34.1"},
    ]
    acc = step(plan(CATALOG, clusters, dt.date(2026, 10, 30)), "aks-acc-sl-az1")
    assert (acc.action, acc.target) == ("upgrade", "1.34.4")


def test_slow_speed_one_month():
    clusters = [{"environment": "dev", "name": "aks-dev-sl-az2", "kubernetes": "1.33.6"}]
    assert step(plan(CATALOG, clusters, TODAY), "aks-dev-sl-az2").action == "none"
    later = step(plan(CATALOG, clusters, dt.date(2026, 10, 21)), "aks-dev-sl-az2")
    assert (later.action, later.target) == ("upgrade", "1.33.7")


def test_minor_upgrade_of_slow_cell():
    clusters = [{"environment": "dev", "name": "aks-dev-sl-az2", "kubernetes": "1.32.9"}]
    s = step(plan(CATALOG, clusters, TODAY), "aks-dev-sl-az2")
    assert (s.action, s.target) == ("upgrade", "1.33.6")


def test_stateful_held_until_lts_window():
    clusters = [{"environment": "dev", "name": "aks-dev-sf", "kubernetes": "1.34.1"}]
    assert step(plan(CATALOG, clusters, dt.date(2026, 6, 1)), "aks-dev-sf").action == "hold"
    s = step(plan(CATALOG, clusters, TODAY), "aks-dev-sf")
    assert (s.action, s.target) == ("upgrade", "1.35.2")


def test_emergency_skips_waiting_but_keeps_order():
    clusters = [
        {"environment": "dev", "name": "aks-dev-sl-az1", "kubernetes": "1.34.5"},
        {"environment": "acc", "name": "aks-acc-sl-az1", "kubernetes": "1.34.4"},
        {"environment": "prd", "name": "aks-prd-sl-az1", "kubernetes": "1.34.4"},
    ]
    steps = plan(CATALOG, clusters, TODAY, emergency=True)
    assert (step(steps, "aks-acc-sl-az1").action, step(steps, "aks-acc-sl-az1").target) == ("upgrade", "1.34.5")
    assert step(steps, "aks-prd-sl-az1").action == "hold"


def test_node_images_and_sl_az3_follows_sl_az2():
    clusters = [
        {
            "environment": "acc",
            "name": "aks-acc-sl-az2",
            "kubernetes": "1.33.7",
            "node_image": "AzureLinux3-202510.01.0",
        },
        {
            "environment": "prd",
            "name": "aks-prd-sl-az3",
            "kubernetes": "1.33.6",
            "node_image": "AzureLinux3-202509.15.0",
        },
    ]
    steps = plan(CATALOG, clusters, dt.date(2027, 1, 15))
    img = step(steps, "aks-prd-sl-az3", "node-image")
    assert (img.action, img.target) == ("upgrade", "AzureLinux3-202510.01.0")
    assert step(steps, "aks-prd-sl-az3").target == "1.33.7"
