from app.kickbase.models import Position
from app.services.sell_guide import GuideWeights, SalePlayer, build_sell_guide

ABW, MF, ST, TW = Position.DEFENDER, Position.MIDFIELDER, Position.FORWARD, Position.GOALKEEPER


def names(option):
    return [p.name for p in option.players]


def test_build_sell_guide_returns_none_when_not_negative():
    assert build_sell_guide(1_000_000, [("A", 5_000_000)], []) is None


def test_build_sell_guide_picks_the_fewest_players():
    squad = [("A", 6_000_000), ("B", 4_000_000), ("C", 2_000_000)]
    guide = build_sell_guide(-10_000_000, squad, [("B", 4_000_000)])
    assert guide.deficit == 10_000_000
    assert guide.fewest.covers
    assert names(guide.fewest) == ["A", "B"]
    assert guide.fewest.total == 10_000_000 and guide.fewest.surplus == 0


def test_build_sell_guide_candidates_may_not_cover():
    squad = [("A", 6_000_000), ("B", 4_000_000), ("C", 2_000_000)]
    guide = build_sell_guide(-10_000_000, squad, [("C", 2_000_000)])
    assert not guide.candidates.covers
    assert names(guide.candidates) == ["C"]
    assert guide.candidates.total == 2_000_000


def test_build_sell_guide_candidates_can_cover():
    squad = [("A", 6_000_000), ("B", 4_000_000)]
    guide = build_sell_guide(-9_000_000, squad, [("A", 6_000_000), ("B", 4_000_000)])
    assert guide.candidates.covers
    assert guide.candidates.total == 10_000_000


def test_build_sell_guide_with_an_empty_squad():
    guide = build_sell_guide(-1_000_000, [], [])
    assert guide.deficit == 1_000_000
    assert guide.best is None
    assert guide.fewest.players == () and not guide.fewest.covers
    assert guide.candidates.players == () and not guide.candidates.covers


def real_squad():
    """The squad of 09.10.2026 (−9,18 Mio.): selling the top player Maza would cover it
    alone, but a bench player and the weakest starter cost far fewer points."""
    return [
        SalePlayer("Maza", 30_850_000, 173, 50, MF),
        SalePlayer("Røssing-Lelesiit", 500_000, 0, 15, ST),
        SalePlayer("Hansen", 3_930_000, 63, 91, ABW),
        SalePlayer("Brassier", 15_160_000, 103, 91, ABW),
        SalePlayer("Regeer", 10_860_000, 120, 91, MF),
        SalePlayer("Prass", 6_840_000, 0, 15, MF),
        SalePlayer("Veerman", 17_090_000, 80, 71, MF),
        SalePlayer("Sano", 20_260_000, 145, 100, MF),
        SalePlayer("Vandevoordt", 16_380_000, 133, 91, TW),
        SalePlayer("Fernández", 15_330_000, 123, 91, MF),
        SalePlayer("Gutiérrez", 25_880_000, 200, 91, ABW),
        SalePlayer("Conté", 10_720_000, 74, 81, ST),
        SalePlayer("Caci", 9_040_000, 150, 91, ABW),
    ]


def test_least_points_lost_keeps_the_top_player():
    squad = real_squad()
    flagged = [p for p in squad if p.name in ("Prass", "Røssing-Lelesiit")]
    guide = build_sell_guide(-9_180_000, squad, flagged)
    assert sorted(names(guide.best)) == ["Hansen", "Prass"]
    assert guide.best.total == 10_770_000 and guide.best.surplus == 1_590_000
    assert guide.best.lost_average == 63
    # With Røssing-Lelesiit as a stand-in the eleven stays complete (3-5-2).
    assert guide.best.empty_after == 0 and guide.best.newly_empty == 0
    # The other ways for comparison: Maza alone, and the candidates that don't cover it.
    assert names(guide.fewest) == ["Maza"] and guide.fewest.lost_average == 173
    assert not guide.candidates.covers


def test_an_empty_slot_counts_like_a_lost_player():
    # Selling the only keeper leaves the eleven without one: worse than a weak field player.
    squad = [
        SalePlayer("Keeper", 5_000_000, 10, 90, TW),
        SalePlayer("Field", 5_000_000, 60, 90, MF),
        *[SalePlayer(f"D{i}", 1_000_000, 100, 90, ABW) for i in range(4)],
        *[SalePlayer(f"M{i}", 1_000_000, 100, 90, MF) for i in range(4)],
        *[SalePlayer(f"S{i}", 1_000_000, 100, 90, ST) for i in range(2)],
    ]
    # Selling "Field" still leaves a full 4-4-2; without the keeper one slot stays empty.
    guide = build_sell_guide(-4_000_000, squad, [])
    assert names(guide.best) == ["Field"]


def test_weights_change_the_choice():
    squad = [
        SalePlayer("Steady", 5_000_000, 50, 100, MF),  # plays every week, average class
        SalePlayer("Star", 5_000_000, 90, 0, MF),  # top class, but out next matchday
    ]
    only_now = GuideWeights(season=0, matchday=1)
    only_class = GuideWeights(season=1, matchday=0)
    assert names(build_sell_guide(-4_000_000, squad, [], only_now).best) == ["Star"]
    assert names(build_sell_guide(-4_000_000, squad, [], only_class).best) == ["Steady"]


def test_no_combination_within_the_limit_covers():
    squad = [SalePlayer(f"P{i}", 1_000_000, 10, 90, MF) for i in range(6)]
    guide = build_sell_guide(-5_000_000, squad, [], GuideWeights(max_players=4))
    assert guide.best is None and guide.fewest.covers  # the value-first way still works


def test_only_slots_emptied_by_the_sale_count_as_new():
    # A squad that is already two players short: selling a bench-less midfielder adds one.
    squad = [SalePlayer("TW", 1_000_000, 50, 90, TW)]
    squad += [SalePlayer(f"D{i}", 1_000_000, 50, 90, ABW) for i in range(4)]
    squad += [SalePlayer(f"M{i}", 2_000_000, 50, 90, MF) for i in range(4)]
    guide = build_sell_guide(-1_500_000, squad, [])
    assert guide.best.empty_after == 3 and guide.best.newly_empty == 1


def test_the_recommendation_never_shows_a_bigger_loss_than_the_others():
    # An injured star (0 % next matchday) vs. a regular starter: both cover the minus.
    squad = [
        SalePlayer("Starter", 10_000_000, 100, 100, MF),
        SalePlayer("Injured", 10_000_000, 140, 0, MF, out=True),
    ]
    guide = build_sell_guide(-5_000_000, squad, [])
    for other in (guide.fewest, guide.candidates):
        if other.players:
            assert guide.best.points_lost <= other.points_lost
    best = guide.best
    assert best.points_lost == round(best.season_loss + best.matchday_loss + best.slot_loss)
