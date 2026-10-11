"""Shared category matching; only declared, nested words may shadow a match."""
import re

from . import catalog


def classify(name, tags, *, require_tag=False):
    parts = {p.strip() for tag in tags for p in re.split(r'[;；,，|>]', tag) if p.strip()}
    parts.difference_update(catalog.NON_BUSINESS_PARENT_TAGS)
    excluded = [w for w in catalog.EXCLUSIONS if w in name or any(w in t for t in parts)]
    if re.search(r'\d+号门', name):
        excluded.append('numbered_gate')
    if excluded:
        return None, 'excluded', ['excluded:' + w for w in excluded]
    full = name + ' ' + ' '.join(parts)
    disputed = [w for w in catalog.REVIEW_TAGS if w in full]
    if disputed:
        return None, 'needs_review', ['policy_unconfirmed:' + w for w in disputed]
    hits = []
    for category in catalog.CATEGORIES:
        for word in category.name_hints:
            hits.extend((category.key, m.start(), m.end()) for m in re.finditer(re.escape(word), name))
    relations = {tuple(pair) for pair in catalog.DATA['nameShadows']}
    original_names = {hit[0] for hit in hits}
    hits = [hit for hit in hits if not any(
        (other[0], hit[0]) in relations and other[1] <= hit[1] and hit[2] <= other[2]
        and other[2] - other[1] > hit[2] - hit[1] for other in hits)]
    names = {hit[0] for hit in hits}
    leaves = {re.split(r'[;；,，|>]', tag)[-1].strip() for tag in tags}
    leaves.difference_update(catalog.NON_BUSINESS_PARENT_TAGS)
    tagged = {c.key for c in catalog.CATEGORIES if leaves.intersection(c.tag_hints)}
    evidence = []
    for c in catalog.CATEGORIES:
        if any(w in name or any(w in tag for tag in parts) for w in c.exclude_hints):
            names.discard(c.key)
            tagged.discard(c.key)
    negative = {key for key in names | tagged if catalog.BY_KEY[key].negative}
    if negative:
        return None, 'excluded', ['negative:' + key for key in sorted(negative)]
    evidence.extend('name:' + catalog.poi_key(key) for key in sorted(names))
    evidence.extend('tag:' + catalog.poi_key(key) for key in sorted(tagged))
    if parts.intersection(catalog.CONFLICTING_TAGS):
        return None, 'needs_review', evidence + ['conflicting_non_target_tags']
    # A declared secondary service may corroborate the primary, never replace it.
    if len(names) == 1:
        primary = next(iter(names))
        secondary = set(catalog.BY_KEY[primary].secondary_categories) | (original_names - names)
        tagged.difference_update(secondary - {primary})
        corroborated = bool(leaves.intersection(catalog.BY_KEY[primary].tag_hints)) or any(
            leaves.intersection(catalog.BY_KEY[key].tag_hints) for key in secondary)
    else:
        corroborated = False
    matches = names | tagged
    if len(matches) > 1:
        return None, 'needs_review', evidence + ['conflicting_categories']
    if len(matches) == 1 and (not require_tag or tagged or corroborated):
        return next(iter(matches)), 'accepted', evidence
    return None, 'needs_review', evidence + ['insufficient_category_evidence']
