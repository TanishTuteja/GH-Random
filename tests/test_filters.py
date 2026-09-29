from app.core.filters import (
    ANY_FILTER_ID,
    LANGUAGE_FILTER_OPTIONS,
    PUSHED_BUCKETS,
    PUSHED_FILTER_OPTIONS,
    STAR_BUCKETS,
    STAR_FILTER_OPTIONS,
    SUPPORTED_LANGUAGES,
)


def test_initial_language_set_contains_ten_unique_options() -> None:
    assert len(SUPPORTED_LANGUAGES) == 10
    assert len({language.id for language in SUPPORTED_LANGUAGES}) == 10


def test_public_filter_options_add_any_without_expanding_crawl_buckets() -> None:
    option_groups = (
        (LANGUAGE_FILTER_OPTIONS, SUPPORTED_LANGUAGES),
        (STAR_FILTER_OPTIONS, STAR_BUCKETS),
        (PUSHED_FILTER_OPTIONS, PUSHED_BUCKETS),
    )

    for public_options, crawl_options in option_groups:
        assert public_options[0].id == ANY_FILTER_ID
        assert public_options[1:] == crawl_options
        assert all(option.id != ANY_FILTER_ID for option in crawl_options)


def test_star_buckets_are_contiguous_and_non_overlapping() -> None:
    for current, following in zip(STAR_BUCKETS, STAR_BUCKETS[1:], strict=False):
        assert current.maximum_stars is not None
        assert current.maximum_stars + 1 == following.minimum_stars

    assert STAR_BUCKETS[0].minimum_stars == 0
    assert STAR_BUCKETS[-1].maximum_stars is None


def test_pushed_buckets_cover_all_non_negative_ages_without_gaps() -> None:
    assert PUSHED_BUCKETS[0].minimum_age_days is None
    assert PUSHED_BUCKETS[-1].maximum_age_days is None

    for current, following in zip(PUSHED_BUCKETS, PUSHED_BUCKETS[1:], strict=False):
        assert current.maximum_age_days == following.minimum_age_days
