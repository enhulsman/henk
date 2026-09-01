"""The named-query registry as reviewable data (§3.1-3.3, 3.8).

From `specs/homelab-tools`: "Named-query tool with a closed query-name enum",
"Query parameter domains", "Templates select by job label and results carry no
addresses", "Threshold comparisons are per-resource and traceable to a
measurement", "No rule-state query in v1", "DNS node identification is derived".

Three of the tests here read a file rather than a literal, on purpose:

- **Parameter domains are compared against the binding spec delta** rather than
  against a second copy typed into this module, so code and spec cannot drift.
- **Every threshold is compared against `notes/backend-probe.md`**, the pinned
  live record. Transcribe-from-prose is this change's recurring defect — three
  instances were caught in review, two of them *inside* fixes written to prevent
  it — so a registry number that matches the docs but not the record must fail.
- **The record's own two tables are cross-checked against each other** (the DNS
  rule thresholds in seconds against the headroom table in milliseconds), so a
  typo in either one of them fails too.

Every parser below carries a coverage assertion. A parser that silently matches
nothing turns a drift-catching test into a green no-op, which is the failure mode
this whole approach exists to avoid.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from henk.tools.query_registry import (
    ADDRESS_BEARING_LABELS,
    CADVISOR_JOBS,
    DNS_METRIC,
    FRESHNESS_TIMESTAMP_METRICS,
    GATUS_WINDOWS,
    NODE_EXPORTER_JOBS,
    NODE_FOR_JOB,
    PROMETHEUS_WINDOWS,
    QUERY_NAMES,
    QUERY_REGISTRY,
    RESOURCES,
    SCRAPE_TARGETS_LOOKBACK,
    QueryBackend,
    describe_target,
    domain_for,
    friendly_target,
    project_labels,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGE = REPO_ROOT / "openspec" / "changes" / "read-depth"
NOTES = (CHANGE / "notes" / "backend-probe.md").read_text()
SPEC = (CHANGE / "specs" / "homelab-tools" / "spec.md").read_text()

#: The six names, from the proposal's table and the spec's closed enum.
EXPECTED_QUERY_NAMES = frozenset(
    {
        "node_resource_trend",
        "scrape_targets",
        "endpoint_history",
        "freshness_check",
        "container_state",
        "dns_performance",
    }
)


# --- Reading the pinned record --------------------------------------------


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _table_rows(text: str, header_fragment: str) -> list[list[str]]:
    """Body rows of the first markdown table whose header holds the fragment."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("|") and header_fragment in line:
            rows = []
            for row in lines[index + 2 :]:
                if not row.startswith("|"):
                    break
                rows.append(_cells(row))
            assert rows, f"table {header_fragment!r} has no body rows"
            return rows
    raise AssertionError(f"no table with header {header_fragment!r} in the record")


def _backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _number(cell: str) -> float:
    match = re.search(r"[\d.]+", cell)
    assert match, f"no number in {cell!r}"
    return float(match.group())


def pinned_resource_bars() -> dict[str, tuple[float, str] | None]:
    """`node_resource_trend`'s bars, from the record's 1.5 pinned table."""
    bars: dict[str, tuple[float, str] | None] = {}
    for resource_cell, bar_cell, _form, source_cell in _table_rows(
        NOTES, "| resource | bar |"
    ):
        resource = _backticked(resource_cell)[0]
        if bar_cell.lower().startswith("none"):
            bars[resource] = None
        else:
            bars[resource] = (_number(bar_cell), _backticked(source_cell)[0])
    assert set(bars) == set(RESOURCES), "the record must pin every resource"
    return bars


def pinned_dns_rule_thresholds() -> dict[tuple[str, str], float]:
    """The seven DNS rules' thresholds in **seconds**, from the 1.4 rule table."""
    out: dict[tuple[str, str], float] = {}
    for row in _table_rows(NOTES, "key metric(s)"):
        names = _backticked(row[1])
        if not names:
            continue
        match = re.fullmatch(r"(Pi5|VPS|Pi2)(High|Critical)DNSProcessingTime", names[0])
        if match:
            out[(match.group(1), match.group(2))] = _number(row[4])
        elif names[0] == "DNSProcessingTimeCritical":
            out[("Fleet", "Critical")] = _number(row[4])
    assert len(out) == 7, f"expected seven DNS rules in the record, got {len(out)}"
    return out


def pinned_dns_headroom() -> dict[str, tuple[float, float, float, float]]:
    """Per-node 24h average and bars in **milliseconds**, from the 1.6 table."""
    out = {}
    for node_cell, avg, warn, crit, fleet in _table_rows(NOTES, "24 h avg"):
        out[_backticked(node_cell)[0]] = (
            _number(avg),
            _number(warn),
            _number(crit),
            _number(fleet),
        )
    assert set(out) == set(NODE_EXPORTER_JOBS), "the record must pin every node"
    return out


def pinned_freshness_metrics() -> tuple[str, ...]:
    """The eight timestamp gauges, from the record's 1.2 fenced list."""
    marker = NOTES.index("**8 timestamp gauges**")
    block = NOTES.index("```", marker)
    end = NOTES.index("```", block + 3)
    names = tuple(NOTES[block + 3 : end].split())
    assert len(names) == 8, f"expected eight metric names, got {len(names)}"
    return names


def pinned_value(label: str) -> str:
    """One row of the record's 1.7 "values the registry must take" table."""
    for value_cell, pinned, _section in _table_rows(NOTES, "| registry value |"):
        if label in value_cell:
            return pinned
    raise AssertionError(f"no pinned value row for {label!r}")


def pinned_marker(slug: str) -> str:
    for marker_cell, _where, pinned, _evidence in _table_rows(NOTES, "| marker |"):
        if slug in marker_cell:
            return pinned
    raise AssertionError(f"no pinned value for APPLY-RESOLVED:{slug}")


# --- Reading the binding spec ---------------------------------------------


def spec_parameter_domains() -> dict[str, dict[str, tuple[str, ...] | None]]:
    """The spec delta's own domain literals, parsed from its normative list.

    A domain of ``None`` means the spec declares it discovered rather than
    enumerated. An `APPLY-RESOLVED:` marker is resolved through the record's
    marker table, exactly as task 9.7 will resolve it in the spec text itself —
    so this test binds both before and after that replacement.
    """
    start = SPEC.index("### Requirement: Query parameter domains")
    section = SPEC[start : SPEC.index("####", start)]
    domains: dict[str, dict[str, tuple[str, ...] | None]] = {}
    for bullet in section.split("\n- ")[1:]:
        text = " ".join(bullet.split())
        names = re.findall(r"^`(\w+)`(?:, `(\w+)`)?:", text)
        assert names, f"unparsed spec bullet: {text[:60]!r}"
        queries = [name for name in names[0] if name]
        entry: dict[str, tuple[str, ...] | None] = {}
        for param, values in re.findall(r"`(\w+)` ∈ \{([^}]*)\}", text):
            entry[param] = tuple(_backticked(values))
        for param, marker in re.findall(r"`(\w+)` ∈ `(APPLY-RESOLVED:[\w-]+)`", text):
            resolved = pinned_marker(marker.split(":", 1)[1])
            entry[param] = tuple(_backticked(resolved)[0].strip("{} ").split(", "))
        for param in re.findall(r"`(\w+)` — discovered", text):
            entry[param] = None
        for query in queries:
            domains[query] = dict(entry)
    assert set(domains) == EXPECTED_QUERY_NAMES, (
        "the spec's domain list must name all six queries; a parser that misses "
        "one turns this whole comparison into a no-op"
    )
    return domains


# --- 3.1 Registry shape ----------------------------------------------------


def test_the_enum_is_exactly_the_six_measuring_queries():
    assert set(QUERY_NAMES) == EXPECTED_QUERY_NAMES
    assert set(QUERY_REGISTRY) == EXPECTED_QUERY_NAMES
    assert len(QUERY_NAMES) == 6
    # QUERY_NAMES is what the schema advertises; it must be derived from the
    # registry rather than typed beside it.
    assert tuple(QUERY_NAMES) == tuple(sorted(QUERY_REGISTRY))


@pytest.mark.parametrize("name", sorted(EXPECTED_QUERY_NAMES))
def test_every_entry_declares_backend_template_domains_and_renderer(name):
    entry = QUERY_REGISTRY[name]
    assert entry.name == name
    assert isinstance(entry.backend, QueryBackend)
    assert entry.templates, "an entry with no template dispatches nothing"
    assert all(isinstance(t, str) and t for t in entry.templates.values())
    # Parameters are a declaration even when empty: `scrape_targets` takes none.
    assert isinstance(entry.parameters, tuple)
    assert callable(entry.renderer)
    # The renderer slot is wired to the matching, clearly named callable, so a
    # copy-paste that points two entries at one renderer fails here.
    assert entry.renderer.__name__ == f"render_{name}"


def test_no_entry_reports_alert_rule_state():
    # "No rule-state query in v1": the closed enum contains only measuring
    # queries. `ALERTS`, the rules API and the alerts API are all absent.
    for entry in QUERY_REGISTRY.values():
        blob = " ".join(entry.templates.values())
        for forbidden in ("ALERTS", "/rules", "/alerts", "alertstate"):
            assert forbidden not in blob, f"{entry.name} reads rule state"
    assert not any("alert" in name for name in QUERY_NAMES)


def test_each_entry_names_the_backend_it_actually_talks_to():
    assert QUERY_REGISTRY["endpoint_history"].backend is QueryBackend.GATUS
    for name in (
        "node_resource_trend",
        "scrape_targets",
        "freshness_check",
        "container_state",
        "dns_performance",
    ):
        assert QUERY_REGISTRY[name].backend is QueryBackend.PROMETHEUS


def test_only_the_range_queries_declare_range_semantics():
    # "Range queries return bounded summaries" names exactly these two.
    ranged = {name for name, e in QUERY_REGISTRY.items() if e.range_query}
    assert ranged == {"node_resource_trend", "dns_performance"}


# --- 3.2 Publication safety: templates ------------------------------------

#: Anything address-shaped, plus the two label selectors the spec names. A
#: template carrying one of these would put a tailnet address in a public repo,
#: and `.githooks/pre-commit` only blocks the literal address — not an
#: `instance=~` selector that would render one into a result.
_ADDRESS_SHAPED = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|://")


@pytest.mark.parametrize("name", sorted(EXPECTED_QUERY_NAMES))
def test_no_registry_template_contains_an_address_or_an_address_selector(name):
    entry = QUERY_REGISTRY[name]
    for template_name, template in entry.templates.items():
        where = f"{name}.{template_name}"
        assert not _ADDRESS_SHAPED.search(template), where
        # Not merely "no `instance=`": the word must not appear at all. The
        # native CPU rule is `avg by (instance)`, which carries no address in the
        # template but renders one into every result.
        assert "instance" not in template, where
        assert "server" not in template, where
        assert "scrapeurl" not in template.lower(), where


def test_dns_performance_selects_by_job_and_never_by_server():
    # The metric's only discriminator is `server`, and it is measured to be an
    # `http://<addr>:<port>` URL — so the mapping is derived at query time from
    # job-labelled series instead (spec: "DNS node identification is derived,
    # never configured or hardcoded").
    entry = QUERY_REGISTRY["dns_performance"]
    templates = " ".join(entry.templates.values())
    assert DNS_METRIC in templates
    assert "server" not in templates
    assert "job=~" in templates or 'job="' in templates, (
        "the node mapping must be derived from a job-labelled series"
    )
    assert entry.derives_node_mapping is True


@pytest.mark.parametrize("name", sorted(EXPECTED_QUERY_NAMES))
def test_every_prometheus_template_selects_on_a_job_label(name):
    entry = QUERY_REGISTRY[name]
    if entry.backend is not QueryBackend.PROMETHEUS:
        return
    if name in ("scrape_targets", "freshness_check", "dns_performance"):
        # These three are fleet-wide by design: bare `up` must enumerate every
        # target, freshness spans two exporters, and the DNS metric's series are
        # separated after the fact by the derived mapping.
        return
    assert any("job=" in template for template in entry.expressions.values()), name


def test_every_declared_job_is_address_free_and_maps_to_a_friendly_name():
    for node, job in {**NODE_EXPORTER_JOBS, **CADVISOR_JOBS}.items():
        assert not _ADDRESS_SHAPED.search(job)
        assert NODE_FOR_JOB[job] == node
        assert friendly_target(job) == node
    # An unknown job is named by its job label rather than guessed at.
    assert friendly_target("pushgateway") == "pushgateway"


# --- 3.2 Publication safety: rendered results ------------------------------

#: A sample as Prometheus and Gatus actually return one, with **placeholder**
#: addresses (10.0.0.x, per the in-repo fixture convention). This is the shape
#: the projection rule exists for: every one of these values is an address.
ADDRESS_BEARING_SAMPLE = {
    "__name__": "node_filesystem_avail_bytes",
    "job": "node-exporter-pi5",
    "instance": "10.0.0.1:9100",
    "mountpoint": "/",
    "server": "http://10.0.0.2:3000",
    "scrapeUrl": "http://10.0.0.1:9100/metrics",
    "globalUrl": "http://10.0.0.1:9100/metrics",
    "__address__": "10.0.0.1:9100",
    "hostname": "10.0.0.3",
    "upstream": "10.0.0.4:53",
}


def test_the_projection_drops_every_address_bearing_label():
    projected = project_labels(ADDRESS_BEARING_SAMPLE)
    rendered = " ".join(f"{k}={v}" for k, v in projected.items())
    for value in ADDRESS_BEARING_SAMPLE.values():
        if _ADDRESS_SHAPED.search(value):
            assert value not in rendered, f"{value} survived the projection"
    for label in ("instance", "server", "scrapeUrl", "globalUrl", "hostname"):
        assert label not in projected
    # And the distinguishing non-address labels the spec names are KEPT, or the
    # projection would be safe and useless.
    assert projected["job"] == "node-exporter-pi5"
    assert projected["mountpoint"] == "/"


def test_the_projection_also_drops_an_unforeseen_label_carrying_an_address():
    # Name-based denial only catches labels someone enumerated. A new exporter
    # label is exactly how the next address would reach a result.
    projected = project_labels(
        {"job": "cadvisor-vps", "endpoint": "http://10.0.0.9:8080", "name": "gatus"}
    )
    assert projected == {"job": "cadvisor-vps", "name": "gatus"}


def test_a_rendered_target_names_the_enum_value_and_no_address():
    rendered = describe_target("node-exporter-pi5", ADDRESS_BEARING_SAMPLE)
    assert "rp5" in rendered
    assert "node-exporter-pi5" in rendered
    assert "mountpoint=/" in rendered
    assert not _ADDRESS_SHAPED.search(rendered)
    for label in ("instance", "server", "scrapeUrl"):
        assert label not in rendered


def test_the_address_bearing_label_set_covers_everything_the_record_names():
    # 1.1 (`hostname`), 1.3 (`scrapeUrl`, `globalUrl`, `labels.instance`,
    # `__address__`) and 1.6 (`server`, `upstream`) each name one.
    for label in (
        "instance",
        "server",
        "scrapeUrl",
        "globalUrl",
        "__address__",
        "hostname",
        "upstream",
    ):
        assert label in ADDRESS_BEARING_LABELS


# --- 3.3 Domains against the binding spec ---------------------------------


def test_every_declared_domain_matches_the_spec_literal():
    for query, params in spec_parameter_domains().items():
        entry = QUERY_REGISTRY[query]
        assert {p.name for p in entry.parameters} == set(params), (
            f"{query}'s parameter names differ from the spec"
        )
        for param, expected in params.items():
            declared = domain_for(query, param)
            if expected is None:
                assert declared is None, f"{query}.{param} must be discovered"
            else:
                assert set(declared) == set(expected), f"{query}.{param}"
                assert len(set(declared)) == len(declared), "duplicate domain value"


def test_the_two_queries_with_no_parameters_declare_none():
    for name in ("scrape_targets", "freshness_check"):
        assert QUERY_REGISTRY[name].parameters == ()


def test_scrape_targets_lookback_is_the_literal_the_spec_names():
    assert SCRAPE_TARGETS_LOOKBACK == "24h"
    assert "SHALL be `24h`" in SPEC
    # Derived, not typed twice: the template is built from the constant.
    assert SCRAPE_TARGETS_LOOKBACK in " ".join(
        QUERY_REGISTRY["scrape_targets"].templates.values()
    )


def test_container_state_excludes_rp2_and_node_resource_trend_includes_it():
    assert set(domain_for("container_state", "node")) == {"rp5", "vps"}
    assert "rp2" in domain_for("node_resource_trend", "node")
    # The domains are per-query because the fleet is not uniform; a shared global
    # node enum is what would make `container_state(rp2)` an empty list.
    assert domain_for("container_state", "node") is not domain_for(
        "node_resource_trend", "node"
    )


def test_the_two_window_vocabularies_are_separate_and_measured():
    # Gatus and Prometheus overlap on exactly {1h, 24h}: the deployed Gatus
    # rejects 15m and 6h, and 7d/30d have no Prometheus counterpart (record 1.1).
    assert set(PROMETHEUS_WINDOWS) == {"15m", "1h", "6h", "24h"}
    assert set(GATUS_WINDOWS) == {"1h", "24h", "7d", "30d"}
    assert set(PROMETHEUS_WINDOWS) & set(GATUS_WINDOWS) == {"1h", "24h"}
    assert set(domain_for("endpoint_history", "window")) == set(GATUS_WINDOWS)
    assert set(domain_for("node_resource_trend", "window")) == set(PROMETHEUS_WINDOWS)
    assert set(domain_for("dns_performance", "window")) == set(PROMETHEUS_WINDOWS)


def test_the_gatus_window_domain_matches_the_pinned_marker():
    resolved = pinned_marker("gatus-window")
    assert set(re.findall(r"\w+", _backticked(resolved)[0])) == set(GATUS_WINDOWS)


def test_dns_performance_names_its_node_parameter_node():
    # Two names for one domain inside a single closed registry is an
    # inconsistency the model will get wrong, so the spec pins the name.
    assert {p.name for p in QUERY_REGISTRY["dns_performance"].parameters} == {
        "node",
        "window",
    }


# --- 3.8 Thresholds against the pinned record ------------------------------


def registry_thresholds() -> dict[tuple[str, str], tuple[float, str]]:
    return {
        (name, key): (t.value, t.source)
        for name, entry in QUERY_REGISTRY.items()
        for key, t in entry.thresholds.items()
    }


def expected_thresholds() -> dict[tuple[str, str], tuple[float, str]]:
    """Everything the record pins, and nothing else."""
    expected: dict[tuple[str, str], tuple[float, str]] = {}
    for resource, bar in pinned_resource_bars().items():
        if bar is not None:
            expected[("node_resource_trend", resource)] = bar
    headroom = pinned_dns_headroom()
    rules = pinned_dns_rule_thresholds()
    prefixes = {"rp5": "Pi5", "vps": "VPS", "rp2": "Pi2"}
    for node, (_avg, warn, crit, fleet) in headroom.items():
        prefix = prefixes[node]
        expected[("dns_performance", f"{node}_warning")] = (
            warn,
            f"{prefix}HighDNSProcessingTime",
        )
        expected[("dns_performance", f"{node}_critical")] = (
            crit,
            f"{prefix}CriticalDNSProcessingTime",
        )
        expected[("dns_performance", "fleet_critical")] = (
            fleet,
            "DNSProcessingTimeCritical",
        )
        # Cross-check the record against itself: the 1.6 headroom table is in
        # milliseconds, the 1.4 rule table in seconds. A typo in either fails.
        assert warn == pytest.approx(rules[(prefix, "High")] * 1000)
        assert crit == pytest.approx(rules[(prefix, "Critical")] * 1000)
        assert fleet == pytest.approx(rules[("Fleet", "Critical")] * 1000)
    return expected


def test_every_registry_threshold_matches_the_pinned_record_and_none_is_invented():
    # Equality in BOTH directions: a value that matches the docs but not the
    # record fails, and so does a threshold the record does not pin at all.
    assert registry_thresholds() == expected_thresholds()


def test_the_memory_bar_is_the_measured_75_not_homelab_healths_90():
    # The obvious 90 is `homelab_health`'s hardcoded constant and the
    # Prometheus-native rule; the Grafana rule that actually delivers reads 75.
    # Three bars are live simultaneously, and this is the one the record pins.
    bar = QUERY_REGISTRY["node_resource_trend"].thresholds["memory"]
    assert bar.value == 75.0
    assert bar.source == "High memory usage"
    assert "75" in pinned_marker("memory-bar")


def test_disk_is_percent_free_below_its_bar_not_percent_used():
    bar = QUERY_REGISTRY["node_resource_trend"].thresholds["disk"]
    assert bar.value == 15.0
    assert bar.direction == "below"
    assert "free" in bar.unit
    template = QUERY_REGISTRY["node_resource_trend"].expressions["disk"]
    assert 'mountpoint="/"' in template
    assert "avail" in template


def test_swap_io_is_the_trigger_and_swap_used_is_labelled_not_the_trigger():
    thresholds = QUERY_REGISTRY["node_resource_trend"].thresholds
    assert thresholds["swap_io"].value == 50.0
    assert thresholds["swap_io"].is_trigger is True
    assert thresholds["swap_used"].value == 95.0
    assert thresholds["swap_used"].is_trigger is False
    # The note is what stops "84% — approaching the 95% bar" being rendered as an
    # approaching incident: fullness and pressure are anti-correlated on this
    # fleet (the vps sits chronically at 64-90% while a Pi at 6% hit 128 pages/s).
    assert thresholds["swap_used"].note


@pytest.mark.parametrize("resource", ["cpu", "load", "temperature"])
def test_resources_with_no_rule_carry_no_bar(resource):
    assert resource not in QUERY_REGISTRY["node_resource_trend"].thresholds
    assert pinned_resource_bars()[resource] is None


def test_the_dns_baselines_are_the_measured_24h_averages():
    baselines = QUERY_REGISTRY["dns_performance"].baselines
    expected = {
        node: avg for node, (avg, _w, _c, _f) in pinned_dns_headroom().items()
    }
    assert baselines == expected
    # No other entry carries a baseline it could compare against silently.
    for name, entry in QUERY_REGISTRY.items():
        if name != "dns_performance":
            assert entry.baselines == {}


def test_scrape_targets_hardcodes_no_target_count():
    # The record says seven today — the six named jobs plus `pushgateway` — but
    # the registry enumerates whatever bare `up` returns. A hardcoded count would
    # be wrong the day a target is added, and a test asserting six would already
    # fail against production.
    entry = QUERY_REGISTRY["scrape_targets"]
    assert entry.thresholds == {}
    assert not hasattr(entry, "expected_target_count")
    assert "**7**" in pinned_value("`scrape_targets` target count")
    numbers = set(re.findall(r"\d+", " ".join(entry.templates.values())))
    assert numbers <= {"1", "24"}, "only the API version and the 24h lookback"
    assert entry.expressions["up"] == "up", "bare `up`, never `up == 0`"


def test_freshness_selects_the_eight_pinned_metric_names():
    assert FRESHNESS_TIMESTAMP_METRICS == pinned_freshness_metrics()
    template = QUERY_REGISTRY["freshness_check"].expressions["timestamps"]
    for metric in FRESHNESS_TIMESTAMP_METRICS:
        assert metric in template
    # A `*_timestamp` glob would silently miss the health-ETL metric, which is
    # the one family using the `_seconds` suffix.
    assert "health_etl_last_success_timestamp_seconds" in template
    assert not any(m.endswith("_timestamp") and "health_etl" in m for m in FRESHNESS_TIMESTAMP_METRICS)


def test_the_dns_metric_is_the_one_the_live_rules_evaluate():
    assert DNS_METRIC == "adguard_avg_processing_time_seconds"
    assert DNS_METRIC in pinned_value("`dns_performance` metric")
    # NOT the per-upstream metric, which carries a fourth address-bearing label.
    assert "top_upstreams" not in " ".join(
        QUERY_REGISTRY["dns_performance"].templates.values()
    )


def test_container_state_declares_creation_semantics_and_its_omissions():
    entry = QUERY_REGISTRY["container_state"]
    expressions = entry.expressions
    assert "container_start_time_seconds" in expressions["created"], (
        "the metric is labelled 'created': it does not move across a restart"
    )
    assert "container_last_seen" in expressions["last_seen"]
    assert "container_oom_events_total" in expressions["oom_events"]
    assert "container_health_state" in expressions["health_state"]
    # A container the query names explicitly must return a reading when its
    # series is absent — cadvisor drops the series entirely when a container
    # stops. This is the fleet's own idiom (DawarichDumpStale, MollySocketLiveness).
    assert "or vector(" in entry.named_container_template
    # The caveats travel with the entry so a renderer cannot forget them.
    assert any("restart" in caveat for caveat in entry.caveats)
    assert any("stopped" in caveat or "removed" in caveat for caveat in entry.caveats)


def test_the_two_measured_availability_holes_are_declared_from_the_record():
    # `temperature` has no series on the vps and `container_health_state` has
    # none on rp5 (record 1.4). Both sit inside domains the spec declares
    # uniform, so both must surface as "in domain, not derivable" rather than as
    # an empty result — and the declaration must come from the record.
    assert "**0 — absent**" in NOTES
    trend = QUERY_REGISTRY["node_resource_trend"]
    assert any(
        u.parameters == {"node": "vps", "resource": "temperature"}
        for u in trend.unavailable
    )
    containers = QUERY_REGISTRY["container_state"]
    assert any(
        u.parameters == {"node": "rp5"} and u.aspect == "health_state"
        for u in containers.unavailable
    )
