"""Options engine (AG-04 numbers): guardrail, protected wholesale stock, constraints, recommendation choice. SYNTHETIC."""
from coworker import options as o

CANDS = [
    {"sku_id": "A-9", "market_id": "US", "daily_rate": 10, "total_available_units": 20, "t1_stockout_risk": True,
     "days_to_stockout": 2, "full_price_usd": 100, "unit_cost_usd": 40, "surplus_units": 0, "size": "9"},
    {"sku_id": "A-9", "market_id": "DE", "daily_rate": 1, "total_available_units": 500, "surplus_units": 300},
    {"sku_id": "A-9", "market_id": "GB", "daily_rate": 1, "total_available_units": 300, "surplus_units": 200},
]
DC = [{"sku_id": "A-9", "market_id": "DE", "location_id": "DC-DE", "available_units": 400},
      {"sku_id": "A-9", "market_id": "GB", "location_id": "DC-GB", "available_units": 250}]
LANES = [
    {"from_location_id": "DC-DE", "to_location_id": "DC-US", "from_market_id": "DE", "to_market_id": "US",
     "mode": "OCEAN", "lead_days": 20, "cost_per_unit_usd": 2},
    {"from_location_id": "DC-DE", "to_location_id": "DC-US", "from_market_id": "DE", "to_market_id": "US",
     "mode": "AIR", "lead_days": 4, "cost_per_unit_usd": 6},
    {"from_location_id": "DC-GB", "to_location_id": "DC-US", "from_market_id": "GB", "to_market_id": "US",
     "mode": "AIR", "lead_days": 3, "cost_per_unit_usd": 12},  # 12% of price: breaches an 8% cap
]
GUARD = {"max_transfer_cost_pct": 0.08}
SIG = {"persistence": 1.0}


def build(**kw):
    args = dict(market_id="US", candidates=CANDS, dc_positions=DC, lanes=LANES, guardrail=GUARD,
                commitments=[], signals=SIG)
    args.update(kw)
    return o.build_options(**args)


def opt(res, oid):
    return next(x for x in res["options"] if x["option_id"] == oid)


def test_need_is_cover_target_minus_available():
    assert build()["needs"][0]["need"] == 10 * 28 - 20


def test_lanes_above_guardrail_are_never_used():
    for x in build()["options"]:
        assert all(ln["cost_per_unit_usd"] / ln["full_price_usd"] <= 0.08 for ln in x["lines"])
        assert "GB" not in x.get("source_markets", [])


def test_open_wholesale_commitments_are_protected():
    res = build(commitments=[{"sku_id": "A-9", "location_id": "DC-DE", "open_units": 350}])
    assert max(x["units"] for x in res["options"]) <= 400 - 350


def test_max_units_constraint_scales_lines():
    res = build(constraints=o.Constraints(max_units=100))
    assert all(x["units"] <= 100 for x in res["options"])


def test_exclude_source_market_leaves_only_do_nothing():
    res = build(constraints=o.Constraints(exclude_source_markets=["DE"]))
    assert [x["option_id"] for x in res["options"]] == ["S0"]
    assert res["recommended_option_id"] == "S0"


def test_fast_lane_beats_slow_lane_when_stockout_is_imminent():
    res = build()
    s1, s2 = opt(res, "S1"), opt(res, "S2")  # same DC pair, different mode: both must survive de-duplication
    assert s1["modes"] == ["OCEAN"] and s2["modes"] == ["AIR"]
    assert s2["net_benefit_usd"] > s1["net_benefit_usd"]  # ocean lands 18 days after the 2-day stock-out
    assert res["recommended_option_id"] in ("S2", "S3")


def test_materiality_and_owner_limit_flags():
    res = build(materiality_gm_usd=1000, approval_limit_units=100)
    s2 = opt(res, "S2")
    assert s2["needs_finance"] and s2["exceeds_owner_limit"]


def test_scale_lines_largest_remainder_keeps_total():
    lines = [{"units": 5}, {"units": 3}, {"units": 2}]
    assert sum(x["units"] for x in o.scale_lines(lines, 7)) == 7


def test_recommendation_json_has_no_lines():
    rj = o.recommendation_json(build())
    assert all("lines" not in x for x in rj["options"]) and rj["recommended_option_id"]


# ---------------------------------------------------------------- store extension (S4 / S5)
STORES = [  # US stores for A-9: NY sells fast and is nearly out, CHI sells slowly with 10 weeks of stock
    {"location_id": "ST-NY", "location_name": "NY Flagship", "market_id": "US", "sku_id": "A-9", "size": "9",
     "demand_rate_daily": 2.0, "available_units": 3},
    {"location_id": "ST-CHI", "location_name": "Chicago", "market_id": "US", "sku_id": "A-9", "size": "9",
     "demand_rate_daily": 0.5, "available_units": 35},
    {"location_id": "ST-LA", "location_name": "LA", "market_id": "US", "sku_id": "A-9", "size": "9",
     "demand_rate_daily": 1.0, "available_units": 14},  # 2 weeks: neither short nor donor
]
STORE_LANES = [{"from_location_id": "ST-CHI", "to_location_id": "ST-NY", "from_market_id": "US", "to_market_id": "US",
                "mode": "TRUCK", "lead_days": 2, "cost_per_unit_usd": 0.9}]


def test_store_short_and_donor_sizing():
    res = build(stores=STORES, store_lanes=STORE_LANES)
    assert [(n["location_id"], n["need"]) for n in res["store_needs"]] == [("ST-NY", 2 * 14 - 3)]
    # donor keeps 4 weeks of its own demand plus 2 display units: 35 - 0.5*28 - 2 = 19
    assert [(d["location_id"], d["units"]) for d in res["donors"]] == [("ST-CHI", 19)]


def test_store_to_store_option_and_combined_recommendation():
    res = build(stores=STORES, store_lanes=STORE_LANES)
    s4, s5 = opt(res, "S4"), opt(res, "S5")
    assert s4["kind"] == "STORE_TRANSFER" and s4["units"] == 19 and s4["donor_stores"] == ["ST-CHI"]
    assert all(x["from_location_id"] == "ST-CHI" and x["to_location_id"] == "ST-NY" for x in s4["lines"])
    assert s5["store_units"] == 19 and s5["units"] == 19 + opt(res, "S2")["units"]  # paired with the best DC option
    assert res["recommended_option_id"] == "S5"
    rj = o.recommendation_json(res)
    assert rj["store_needs"][0]["location_id"] == "ST-NY" and rj["donors"][0]["spare_units"] == 19


def test_excluded_donor_store_removes_store_options():
    res = build(stores=STORES, store_lanes=STORE_LANES, constraints=o.Constraints(exclude_source_locations=["ST-CHI"]))
    assert not {"S4", "S5"} & {x["option_id"] for x in res["options"]}


def test_store_moves_ignore_sizes_not_at_risk_and_other_markets():
    other = [dict(s, sku_id="B-1") for s in STORES] + [dict(STORES[1], market_id="CA", location_id="ST-TOR")]
    res = build(stores=other, store_lanes=STORE_LANES)
    assert not res["store_needs"] and not res["donors"]


def test_store_view_counts_units_already_on_the_way():
    from coworker import evidence
    gold = [{"location_id": "ST-B", "sku_id": "K", "available_units": 1}]
    live = [{"location_id": "ST-B", "sku_id": "K", "available_units": 2}]
    out = evidence.store_view(gold, live, [{"to_location_id": "ST-B", "sku_id": "K", "units": 5}])
    assert out[0]["available_units"] == 7 and out[0]["incoming_units"] == 5
