import { describe, expect, it } from 'vitest';
import { getBaiduSession, getHybridSession, saveBaiduSession, saveHybridSession } from './algorithmSessions';

describe('algorithm sessions', () => {
  it('keeps the two algorithm snapshots independent', () => {
    const baidu = getBaiduSession();
    const hybrid = getHybridSession();
    saveBaiduSession({ ...baidu, dirty: true });
    saveHybridSession({ ...hybrid, dirty: false });

    expect(getBaiduSession().dirty).toBe(true);
    expect(getHybridSession().dirty).toBe(false);

    saveBaiduSession(baidu);
    saveHybridSession(hybrid);
  });
});
