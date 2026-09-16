import time
import pytest
from tools.multicross_low_budget_live import LowBudgetLedger
from tools.live_smoke import LiveGuardError
from test_multicross_live import route_request

@pytest.mark.parametrize('budget',[200,300])
def test_last_slot_hard_limit_and_no_restart(tmp_path,budget):
    with LowBudgetLedger(tmp_path,budget) as ledger:
        ledger.arm()
        ledger.data['counts']['analysis']=budget-1
        ledger.save()
        ledger.reserve('analysis',route_request(0),time.monotonic())
        assert ledger.data['counts']['analysis']==budget
        with pytest.raises(LiveGuardError):ledger.reserve('analysis',route_request(1),time.monotonic())
        assert 'SECRET_TEST_ONLY' not in ledger.file.read_text()
    with LowBudgetLedger(tmp_path,budget) as ledger:
        with pytest.raises(LiveGuardError):ledger.arm()

def test_budget_outside_fixed_two_arms_rejected(tmp_path):
    with pytest.raises(ValueError):LowBudgetLedger(tmp_path,400)
