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
          { label: '百度边界搜索（E8.2）', value: 'baidu' },
          { label: 'OSM＋百度', value: 'hybrid' },
        ]}
      />
    </nav>
    {algorithm === 'baidu' ? <BaiduApp /> : <HybridApp />}
  </>;
}
