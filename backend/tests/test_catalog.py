"""§4.3/§10: one category dictionary, however many vocabularies spell it.

These are guards, not a re-statement of the data: each one compares the single
dictionary against a literal or a table that some other module still has to
declare for the type system (a ``Literal`` cannot be derived from a JSON file
without giving up static checking). If a category is added or renamed in only
one place, one of these fails instead of the two quietly diverging.
"""
from pathlib import Path
from typing import get_args

from app import catalog
from app.contracts import MINOR_TO_MAJOR, MajorCategory, MinorCategory
from app.facilities import GROUPS
from app.places import QUERIES, classify
from app.poi.models import CATEGORIES as POI_CATEGORIES
from app.poi.models import Category as PoiCategory


def test_the_contracts_are_a_projection_of_the_dictionary():
    assert get_args(MinorCategory) == catalog.keys()
    assert MINOR_TO_MAJOR == {c.key: c.major for c in catalog.CATEGORIES}
    assert get_args(MajorCategory) == catalog.majors()
    assert GROUPS == {major: catalog.minors_of(major) for major in catalog.majors()}


def test_the_poi_runtime_spelling_is_an_alias_of_one_category():
    # ``primary_school`` is this runtime's name for ``school``: one category, so
    # it is declared once, next to the canonical key, and never as a second row.
    assert get_args(PoiCategory) == POI_CATEGORIES
    assert POI_CATEGORIES == tuple(catalog.poi_key(key) for key in catalog.POI_RUNTIME)
    assert catalog.POI_RUNTIME == tuple(c.key for c in catalog.CATEGORIES if c.poi_runtime)
    assert len(catalog.POI_RUNTIME) == 31
    assert catalog.poi_key("school") == "primary_school"
    assert catalog.poi_key("market") == "market"
    assert len(catalog.BY_KEY) == len(catalog.CATEGORIES)
    for category in catalog.CATEGORIES:
        assert category.major in catalog.majors()
        assert category.label


def test_the_legacy_request_types_are_the_primary_queries():
    assert QUERIES == {c.key: c.query for c in catalog.CATEGORIES}
    # The first request type is the primary one, and the rest extend it.
    for category in catalog.CATEGORIES:
        assert category.queries()[0] == category.query
        assert len(set(category.queries())) == len(category.queries())


def test_every_request_type_can_also_identify_its_own_category_by_name():
    # Otherwise a search's own keyword would return records its own name rule
    # refuses, and the result would read as unclassified rather than as a hit.
    for category in catalog.CATEGORIES:
        for keyword in category.queries():
            assert any(keyword in name for name in category.name_hints), category.key


def test_only_the_declared_pair_of_categories_shares_name_words():
    # ``医院药房`` contains ``药房``, which is why the hospital dispensary has to
    # shadow the general pharmacy. Any *other* overlap would let one name match
    # two categories with no rule to resolve it.
    overlapping = []
    for left in catalog.CATEGORIES:
        for right in catalog.CATEGORIES:
            if left.key >= right.key:
                continue
            if any(w in other or other in w
                   for w in left.name_hints for other in right.name_hints):
                overlapping.append({left.key, right.key})
    assert {frozenset(pair) for pair in overlapping} == {
        frozenset(pair) for pair in catalog.DATA["nameShadows"]}


def test_the_exclusion_vocabulary_is_declared_once_and_is_not_empty():
    assert len(catalog.EXCLUSIONS) == len(set(catalog.EXCLUSIONS))
    # A gate is never a facility, and a training business is never a school.
    for word in ("门口", "北门", "管理办公室"):
        assert word in catalog.EXCLUSIONS
    for word in ("教育培训",):
        assert word in catalog.NON_BUSINESS_PARENT_TAGS
    assert catalog.POSSIBLE_DUPLICATE_METERS > 0


def test_the_poi_rules_view_is_keyed_by_that_runtimes_names_only():
    rules = catalog.poi_rules()
    assert rules is catalog.poi_rules()
    assert set(rules["queries"]) == set(rules["supportedTags"]) == set(rules["nameHints"])
    assert set(rules["queries"]) == set(POI_CATEGORIES)
    assert rules["version"] == catalog.VERSION
    assert "school" not in rules["queries"] and "primary_school" in rules["queries"]


def test_no_second_category_file_is_shipped():
    # The dictionary is the only list; a leftover file would be a second one.
    assert catalog.CATALOG_PATH.is_file()
    assert not Path(catalog.CATALOG_PATH.parent, "poi", "categories.json").exists()


def test_both_consumers_classify_the_same_vocabulary_the_same_way():
    # The legacy endpoint classifies name and tag together; the POI runtime
    # separates them. They must still agree on what a name alone means.
    from app.poi.normalize import classify as strict
    for category in catalog.CATEGORIES:
        for name in category.name_hints:
            result, status, _ = strict(f"合成{name}", list(category.tag_hints[:1]))
            if status == 'accepted':
                assert catalog.contract_key(result) == classify(f"合成{name}", category.tag_hints[0])
    assert catalog.VERSION == "poi-categories-v2.2"
