import { describe, expect, it } from 'vitest';
import { HybridApiError } from './client';
import { classifyHybridError, describeHybridError } from './controller';

describe('an offline deployment is a refusal, not a connection problem', () => {
  it('names the offline mode instead of sending the reader to check the network', () => {
    const text = describeHybridError(new HybridApiError('synthetic_mode_offline', 503));
    expect(text).toContain('离线合成模式');
    // Before the message existed this code fell through to "请检查服务连接后重试",
    // which points a deployment decision at the reader's network.
    expect(text).not.toContain('请检查服务连接');
  });

  it('classifies it as refused rather than as a failure of the request itself', () => {
    expect(classifyHybridError(new HybridApiError('synthetic_mode_offline', 503))).toBe('refused');
  });
});
