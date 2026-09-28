from app.core.filters import ACTIVITY_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES


def test_initial_language_set_contains_ten_unique_options() -> None:
    assert len(SUPPORTED_LANGUAGES) == 10
    assert len({language.id for language in SUPPORTED_LANGUAGES}) == 10


def test_star_buckets_are_contiguous_and_non_overlapping() -> None:
    for current, following in zip(STAR_BUCKETS, STAR_BUCKETS[1:], strict=False):
        assert current.maximum_stars is not None
        assert current.maximum_stars + 1 == following.minimum_stars

    assert STAR_BUCKETS[0].minimum_stars == 0
    assert STAR_BUCKETS[-1].maximum_stars is None


def test_activity_buckets_cover_all_non_negative_ages_without_gaps() -> None:
    assert ACTIVITY_BUCKETS[0].minimum_age_days is None
    assert ACTIVITY_BUCKETS[-1].maximum_age_days is None

    for current, following in zip(ACTIVITY_BUCKETS, ACTIVITY_BUCKETS[1:], strict=False):
        assert current.maximum_age_days == following.minimum_age_days
