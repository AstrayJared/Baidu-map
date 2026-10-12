"""The archived forty-facility run, distilled to its frozen route evidence."""
import json
from pathlib import Path

from app.checkups.verification_stage import evidence_counts
from app.checkups.reporting_stage import _verification_section


def test_archived_routes_keep_returned_routes_separate_from_decision_layers():
    path = Path(__file__).parent / 'fixtures' / 'checkup-verification-40.json'
    records = json.loads(path.read_text(encoding='utf-8'))
    assert len(records) == 40
    assert evidence_counts(records) == {
        'route_returns': 38,
        'strict_confirmed': 0,
        'tolerance_estimated': 33,
        'no_usable_decision': 7,
    }
    assert all(item['poiStatus'] == 'pending' for item in records)
    legacy = _verification_section({'status': 'partial', 'checked': 40,
                                    'facilities': records})
    assert (legacy.route_returns, legacy.strict_confirmed,
            legacy.tolerance_estimated, legacy.no_usable_decision) == (38, 0, 33, 7)
