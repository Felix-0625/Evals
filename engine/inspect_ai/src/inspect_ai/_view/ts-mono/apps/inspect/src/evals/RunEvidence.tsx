import { Link } from "react-router";

import { baseUrl } from "../app/routing/url";

import { dimensionNames, percentage, record, records, text } from "./data";

/** Read-only evidence view; controls stay with the experiment page. */
export function RunEvidence({ runs }: { runs: unknown }) {
  return (
    <>
      {records(runs).map((run, index) => {
        const trials: unknown[] = Array.isArray(run.trials) ? run.trials : [];
        const first = trials[0] === undefined ? undefined : record(trials[0]);
        const summary = record(run.summary);
        const repetitions: unknown[] = Array.isArray(summary.repetitions)
          ? summary.repetitions
          : [];
        return (
          <details key={index}>
            <summary>
              {first ? text(first.candidate_id) : "运行"} · {trials.length}{" "}
              轮判定
            </summary>
            <table className="table table-sm">
              <thead>
                <tr>
                  <th>维度</th>
                  <th>适用检查</th>
                  <th>通过 / 失败</th>
                  <th>无法核验 / 故障 / 未执行</th>
                  <th>不适用</th>
                  <th>已确认通过</th>
                  <th>有效通过率</th>
                  <th>覆盖率</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(record(summary.dimensions)).map(
                  ([dimension, raw]) => {
                    const item = record(raw);
                    const counts = record(item.counts);
                    return (
                      <tr key={dimension}>
                        <td>{dimensionNames[dimension] ?? dimension}</td>
                        <td>{String(item.applicable)}</td>
                        <td>
                          {String(counts.pass)} / {String(counts.fail)}
                        </td>
                        <td>
                          {String(counts.unverifiable)} /{" "}
                          {String(counts.grading_error)} /{" "}
                          {String(counts.not_run)}
                        </td>
                        <td>{String(counts.not_applicable)}</td>
                        <td>{percentage(item.confirmed_pass_ratio)}</td>
                        <td>{percentage(item.valid_pass_rate)}</td>
                        <td>{percentage(item.coverage)}</td>
                      </tr>
                    );
                  }
                )}
              </tbody>
            </table>
            <details>
              <summary>业务分组与重复运行波动</summary>
              <p>
                独立样本 {String(summary.independent_samples)} · 业务组{" "}
                {String(summary.groups)} · 总轮次 {String(summary.rounds)}
              </p>
              <table className="table table-sm">
                <thead>
                  <tr>
                    <th>样本</th>
                    <th>轮次</th>
                    <th>结果通过率</th>
                    <th>观测情况</th>
                  </tr>
                </thead>
                <tbody>
                  {repetitions.map((raw) => {
                    const item = record(raw);
                    return (
                      <tr key={text(item.sample_id)}>
                        <td>{text(item.sample_id)}</td>
                        <td>{JSON.stringify(item.epochs)}</td>
                        <td>
                          {percentage(record(item.result).confirmed_pass_ratio)}
                        </td>
                        <td>
                          {item.missing_results === true
                            ? "存在缺失判定"
                            : item.mixed_outcomes === true
                              ? "轮次间成功/失败波动"
                              : "已观测轮次结果一致"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <details>
                <summary>各业务组的八维覆盖与费用</summary>
                <pre>{JSON.stringify(summary.by_group, null, 2)}</pre>
              </details>
            </details>
            {trials.map((raw, trialIndex) => {
              const trial = record(raw);
              return (
                <p key={trialIndex}>
                  <Link
                    to={baseUrl(
                      text(trial.log),
                      text(trial.sample_id),
                      String(trial.epoch)
                    )}
                  >
                    {text(trial.sample_id)} / epoch {String(trial.epoch)} →
                    Inspect 样本与事件
                  </Link>
                </p>
              );
            })}
          </details>
        );
      })}
    </>
  );
}
