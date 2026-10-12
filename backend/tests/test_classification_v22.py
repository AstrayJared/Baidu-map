import pytest

from app import catalog
from app.places import classify as legacy
from app.poi.normalize import classify


@pytest.mark.parametrize('name,tag,primary', [
    ('医院药房', '药店', 'hospital_pharmacy'),
    ('社区医院', '社区卫生服务', 'clinic'),
    ('康复医院', '康复', 'rehab'),
    ('生鲜市场', '农贸市场', 'market'),
    ('生鲜超市', '生鲜', 'fresh_store'),
    ('小学部', '小学', 'combined_school'),
    ('初中部', '初中', 'combined_school'),
    ('美术馆', '博物馆', 'library'),
    ('合成小学', '教育培训;小学', 'school'),
])
def test_declared_nested_words_choose_one_primary(name, tag, primary):
    result, status, _ = classify(name, [tag])
    assert status == 'accepted'
    assert catalog.contract_key(result) == primary
    assert legacy(name, tag) == primary
    assert legacy(name) == primary


@pytest.mark.parametrize('name,tags', [
    ('社区药店', ['小学']),
    ('合成药店', ['医疗;药店', '教育;中学']),
    ('医院药房及医院', ['医院药房']),
    ('小学部及小学', ['小学部']),
    ('合成社区医院及人民医院', ['社区卫生服务']),
])
def test_independent_conflicts_are_not_removed_by_priority(name, tags):
    assert classify(name, tags)[1] == 'needs_review'
    assert legacy(name, ';'.join(tags)) is None


@pytest.mark.parametrize('name,tag', [
    ('美术馆培训班', '博物馆'), ('小学辅导班', '教育培训;小学'),
    ('合成小学', '教育培训;培训机构'),
])
def test_independent_negative_evidence_still_excludes(name, tag):
    assert classify(name, [tag])[1] == 'excluded'
    assert legacy(name, tag) is None


def test_declared_secondary_services_do_not_duplicate_primary():
    assert catalog.secondary_categories_for('hospital_pharmacy') == ('pharmacy',)
    assert len(catalog.majors()) == 10
    assert len(catalog.display_group_keys()) == 5
