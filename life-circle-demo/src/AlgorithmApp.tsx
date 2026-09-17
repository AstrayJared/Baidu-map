import { useState } from 'react';
import { Segmented } from 'antd';
import BaiduApp from './analysis/ApiApp';
import HybridApp from './hybrid/HybridApp';

type Algorithm = 'baidu' | 'hybrid';

export default function AlgorithmApp() {
  const configured = import.meta.env.VITE_ANALYSIS_MODE === 'hybrid' ? 'hybrid' : 'baidu';
  const [algorithm, setAlgorithm] = useState<Algorithm>(configured);
  return <>
    <nav className="algorithm-switch" aria-label="算法选择">
      <Segmented<Algorithm>
        aria-label="算法选择"
        value={algorithm}
        onChange={setAlgorithm}
        options={[
          { label: '纯百度（更快）', value: 'baidu' },
          { label: 'OSM＋百度（更准确）', value: 'hybrid' },
        ]}
      />
    </nav>
    {algorithm === 'baidu' ? <BaiduApp /> : <HybridApp />}
  </>;
}
