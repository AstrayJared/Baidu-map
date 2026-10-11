"""Frozen request directory is reported even when assessment rows are absent."""
from app import catalog
from app.checkups.reporting_stage import build_report


def test_report_freezes_all_ten_requested_categories_and_names_missing_rows():
    report = build_report(task_id='offline', revision=1, source_result_hash='hash',
                          generated_at=1.0, domain=None, domain_area_m2=None,
                          accessibility=None, service_gaps=None, heatmap=None, scores=None,
                          facilities={'queryStatus': 'partial'},
                          requested_categories=catalog.majors())
    assert report.category_directory_version == catalog.VERSION
    assert [item['id'] for item in report.category_directory] == list(catalog.majors())
    assert len(report.categories) == 10
    assert all(not row.supported and row.unavailable_reason == 'query_incomplete'
               for row in report.categories)


def test_completed_empty_search_is_distinct_from_unfinished_search():
    report = build_report(task_id='offline', revision=1, source_result_hash='hash',
                          generated_at=1.0, domain=None, domain_area_m2=None,
                          accessibility=None, service_gaps=None, heatmap=None, scores=None,
                          facilities={'queryStatus': 'completed'},
                          requested_categories=('medical',))
    assert report.categories[0].unavailable_reason == 'no_usable_evidence'
