"""The one category dictionary: request types, classification vocabulary, aliases.

§4.3 requires one authoritative backend dictionary, and §10 forbids hand
maintaining several inconsistent category and request-type lists. Every consumer
reads this module and nothing else:

* the contract vocabulary (``major``/``minor`` categories in ``contracts.py``,
  used by ``/api/analyses``, ``places.py`` and the v2 checkup snapshot), and
* the POI runtime's vocabulary, which spells the education category
  ``primary_school`` for historical reasons.

``primary_school`` is declared here as that runtime's own spelling of ``school``:
one category with a name per consumer, never two categories. The alias is applied
by :func:`poi_rules`, so no POI module keeps a private list.

Each category carries three word lists, and they are not interchangeable:

``query`` / ``extraQueries``
    Request types sent to the place service. The legacy endpoint sends only
    ``query``; the POI runtime sends the whole list in this order.
``nameHints``
    Words that identify the category from a facility's name.
``tagHints``
    Words that identify it from the provider's own classification tag.

The vocabulary is data (``categories.json``) so it can be reviewed and versioned
on its own; this module is the only reader.
"""
import json
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("categories.json")


@dataclass(frozen=True)
class Category:
    """One minor category, named once, however many vocabularies spell it."""
    key: str
    major: str
    label: str
    query: str
    poi_runtime: bool = False
    poi_name: str | None = None
    extra_queries: tuple[str, ...] = ()
    name_hints: tuple[str, ...] = ()
    tag_hints: tuple[str, ...] = ()
    exclude_hints: tuple[str, ...] = ()
    display_group: str | None = None
    priority: int = 0
    negative: bool = False
    secondary_categories: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()

    def queries(self) -> tuple[str, ...]:
        """Every request type for this category, primary one first."""
        return (self.query, *self.extra_queries)


def _read() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


DATA = _read()
VERSION: str = DATA["version"]


@dataclass(frozen=True)
class DisplayGroup:
    key: str
    label: str
    order: int
    majors: tuple[str, ...]


DISPLAY_GROUPS: tuple[DisplayGroup, ...] = tuple(
    DisplayGroup(
        key=item["key"], label=item["label"], order=item.get("order", index + 1),
        majors=tuple(item.get("majors", ())),
    )
    for index, item in enumerate(DATA.get("displayGroups", ()))
)
DISPLAY_GROUP_BY_KEY: dict[str, DisplayGroup] = {item.key: item for item in DISPLAY_GROUPS}
MAJOR_TO_DISPLAY_GROUP: dict[str, str] = {
    major: group.key for group in DISPLAY_GROUPS for major in group.majors
}

MAJOR_LABELS: dict[str, str] = {
    "medical": "医疗健康", "shopping": "购物消费", "education": "教育",
    "care": "疗养康养", "dining": "餐饮", "finance": "金融",
    "public": "政务公共服务", "leisure": "文体休闲",
    "transport": "交通出行", "life": "生活服务",
}

CATEGORIES: tuple[Category, ...] = tuple(
    Category(
        key=item["key"], major=item["major"], label=item["label"], query=item["query"],
        poi_runtime=item.get("poiRuntime", False), poi_name=item.get("poiName"),
        extra_queries=tuple(item.get("extraQueries", ())),
        name_hints=tuple(item["nameHints"]), tag_hints=tuple(item["tagHints"]),
        exclude_hints=tuple(item.get("excludeHints", ())),
        display_group=item.get("displayGroup"), priority=int(item.get("priority", 0)),
        negative=bool(item.get("negative", False)),
        secondary_categories=tuple(item.get("secondaryCategories", ())),
        capabilities=tuple(item.get("capabilities", ())),
    ) for item in DATA["categories"])

# The catalogue contains the full supported taxonomy. Keep the default analysis
# scope separate so adding a category does not silently multiply every request.
DEFAULT_ANALYSIS_MAJORS: tuple[str, ...] = ("shopping", "medical", "education")
_CATALOG_MAJORS = {category.major for category in CATEGORIES}
if not set(DEFAULT_ANALYSIS_MAJORS).issubset(_CATALOG_MAJORS):
    raise RuntimeError("DEFAULT_ANALYSIS_MAJORS contains an unknown catalogue major")

BY_KEY: dict[str, Category] = {category.key: category for category in CATEGORIES}
# The POI runtime's own spelling for a category, where it differs.
ALIASES: dict[str, str] = {c.key: c.poi_name for c in CATEGORIES if c.poi_name}
# Which categories that runtime collects at all.
POI_RUNTIME: tuple[str, ...] = tuple(c.key for c in CATEGORIES if c.poi_runtime)

EXCLUSIONS: tuple[str, ...] = tuple(DATA["excluded"])
NON_BUSINESS_PARENT_TAGS: frozenset[str] = frozenset(DATA["nonBusinessParentTags"])
CONFLICTING_TAGS: frozenset[str] = frozenset(DATA["conflictingTags"])
REVIEW_TAGS: tuple[str, ...] = tuple(DATA["review"])
POSSIBLE_DUPLICATE_METERS: float = DATA["possibleDuplicateMeters"]


def keys() -> tuple[str, ...]:
    return tuple(category.key for category in CATEGORIES)


def queries(key: str) -> tuple[str, ...]:
    return BY_KEY[key].queries()


def majors() -> tuple[str, ...]:
    """Major categories in the order their first minor category appears."""
    return tuple(dict.fromkeys(category.major for category in CATEGORIES))


def default_analysis_majors() -> tuple[str, ...]:
    """Stable default scope for analysis and checkup requests."""
    return DEFAULT_ANALYSIS_MAJORS


def major_label(major: str) -> str:
    return MAJOR_LABELS.get(major, major)


def major_directory(selected=None) -> list[dict]:
    """Public, ordered view of the authoritative request-category directory."""
    return [{"id": major, "label": major_label(major), "order": index}
            for index, major in enumerate(majors())
            if selected is None or major in selected]


def minors_of(major: str) -> tuple[str, ...]:
    return tuple(category.key for category in CATEGORIES if category.major == major)


def display_group_keys() -> tuple[str, ...]:
    return tuple(group.key for group in sorted(DISPLAY_GROUPS, key=lambda item: item.order))


def display_group_for_major(major: str) -> str | None:
    return MAJOR_TO_DISPLAY_GROUP.get(major)


def display_group_for_minor(name: str) -> str | None:
    category = BY_KEY.get(contract_key(name))
    return category.display_group if category and category.display_group else (
        display_group_for_major(category.major) if category else None
    )


def display_group_label(key: str) -> str | None:
    group = DISPLAY_GROUP_BY_KEY.get(key)
    return group.label if group else None


def secondary_categories_for(name: str) -> tuple[str, ...]:
    category = BY_KEY.get(contract_key(name))
    return category.secondary_categories if category else ()


def capabilities_for(name: str) -> tuple[str, ...]:
    category = BY_KEY.get(contract_key(name))
    return category.capabilities if category else ()


def catalog_payload() -> dict:
    """Stable read-only payload used by clients that need the current taxonomy."""
    return {
        "version": VERSION,
        "displayGroups": [
            {"key": group.key, "label": group.label, "order": group.order, "majors": list(group.majors)}
            for group in sorted(DISPLAY_GROUPS, key=lambda item: item.order)
        ],
        "categories": [
            {
                "key": category.key,
                "major": category.major,
                "displayGroup": category.display_group or display_group_for_major(category.major),
                "label": category.label,
                "query": category.query,
                "extraQueries": list(category.extra_queries),
                "priority": category.priority,
                "secondaryCategories": list(category.secondary_categories),
                "capabilities": list(category.capabilities),
            }
            for category in CATEGORIES
        ],
    }


def facility_categories() -> dict:
    """设施目录的 v2 视图：五个展示组、十个大类，以及每个大类展开成几个检索小类。

    小类数是一次设施检索的算术分子（首轮下界 = 查询分块数 × 检索小类数），界面必须
    能在提交前算出选中的类别够不够预算，而不是提交后拿到一个预算不足的失败任务。
    核心三类是总体分的口径，所以它由后端点名，不由界面自己写死三个名字。
    """
    core = default_analysis_majors()
    return {
        "version": VERSION,
        "coreMajors": list(core),
        "displayGroups": [
            {"key": group.key, "label": group.label, "order": group.order,
             "majors": list(group.majors)}
            for group in sorted(DISPLAY_GROUPS, key=lambda item: item.order)],
        "majors": [
            {"key": major, "label": major_label(major),
             "displayGroup": display_group_for_major(major),
             "core": major in core,
             "minorCategories": len(poi_keys((major,)))}
            for major in majors()],
    }


def poi_key(key: str) -> str:
    """The POI runtime's spelling of a category; every other name is unchanged."""
    return ALIASES.get(key, key)


# The reverse view, built from the same table so a caller holding the runtime's
# spelling gets the contract's category back without a second list.
_KEY_BY_POI_NAME: dict[str, str] = {poi_key(category.key): category.key for category in CATEGORIES}


def contract_key(name: str) -> str:
    """The contract's key for a name in either vocabulary."""
    return _KEY_BY_POI_NAME.get(name, name)


def major_of(name: str) -> str | None:
    """The major category a minor category belongs to, in either spelling."""
    category = BY_KEY.get(contract_key(name))
    return category.major if category is not None else None


def poi_keys(majors=None) -> tuple[str, ...]:
    """The POI runtime's categories for these major categories, in dictionary order.

    This is the one place the request's category vocabulary is translated into
    the retrieval runtime's, so a request cannot name a category the dictionary
    does not have and no module keeps its own mapping.
    """
    return tuple(poi_key(category.key) for category in CATEGORIES
                 if category.poi_runtime and (majors is None or category.major in majors))


def _mapping(selector) -> dict:
    return {poi_key(category.key): selector(category) for category in CATEGORIES
            if category.poi_runtime}


# The POI runtime's view: only the categories it collects, under its own names,
# in the shape its planner and normalizer already read.
POI_RULES: dict = {
    "version": VERSION,
    "displayGroups": [
        {"key": group.key, "label": group.label, "order": group.order, "majors": list(group.majors)}
        for group in sorted(DISPLAY_GROUPS, key=lambda item: item.order)
    ],
    "queries": _mapping(lambda c: list(c.queries())),
    "supportedTags": _mapping(lambda c: list(c.tag_hints)),
    "nameHints": _mapping(lambda c: list(c.name_hints)),
    "nameShadows": [[poi_key(specific), poi_key(generic)]
                    for specific, generic in DATA["nameShadows"]],
    "excludeHints": _mapping(lambda c: list(c.exclude_hints)),
    "displayGroupByCategory": _mapping(lambda c: c.display_group or display_group_for_major(c.major)),
    "priorities": _mapping(lambda c: c.priority),
    "secondaryCategories": _mapping(lambda c: list(c.secondary_categories)),
    "capabilities": _mapping(lambda c: list(c.capabilities)),
    "negative": _mapping(lambda c: c.negative),
    "excluded": list(EXCLUSIONS),
    "nonBusinessParentTags": sorted(NON_BUSINESS_PARENT_TAGS),
    "conflictingTags": sorted(CONFLICTING_TAGS),
    "review": list(REVIEW_TAGS),
    "possibleDuplicateMeters": POSSIBLE_DUPLICATE_METERS,
}


def poi_rules() -> dict:
    return POI_RULES
