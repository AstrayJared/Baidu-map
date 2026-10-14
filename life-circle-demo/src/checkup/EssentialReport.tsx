import type { EssentialServicesEvidence } from './contract';
import { area } from './report';

export const ESSENTIAL_LABELS: Record<string, string> = { market: '菜市场', pharmacy: '药店', primary_school: '小学' };
export default function EssentialReport({ value }: { value?: EssentialServicesEvidence | null }) {
  return <section className="rp-sec" data-testid="essential-report">
    <h2>基础三项专项</h2>
    {!value ? <p>本修订未评估菜市场、药店、小学专项。</p> : <>
      <p>菜市场、独立药店、小学分别检查 1000 米步行服务；模型推定，不进入三大类总体分。</p>
      <table><thead><tr><th>专项</th><th>已知覆盖</th><th>疑似缺口</th><th>未知</th></tr></thead>
        <tbody>{value.accessibility.categories.map(row => <tr key={row.category}>
          <th>{ESSENTIAL_LABELS[row.category] ?? row.category}</th>
          {row.supported ? <><td>{area(row.coveredM2)}</td><td>{area(row.gapM2)}</td><td>{area(row.unknownM2)}</td></>
            : <td colSpan={3}>未评估：{row.unavailableReason}</td>}
        </tr>)}</tbody></table>
      <p>疑似缺口表示相关范围与关键词下未检索到可用服务，目录尚未经独立核实。有候选但入口不明时保留未知；大类路线抽检不等于专项实测。</p>
    </>}
  </section>;
}
