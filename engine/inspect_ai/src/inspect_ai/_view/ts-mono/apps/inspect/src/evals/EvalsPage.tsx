import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { record, records, text } from "./data";
import styles from "./EvalsPage.module.css";
import { RunEvidence } from "./RunEvidence";

async function request(path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(`/api/evals${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Inspect-View-Request": "true",
      "Idempotency-Key": crypto.randomUUID(),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result: unknown = await response.json();
  if (!response.ok) throw new Error(JSON.stringify(result));
  return result;
}

export function EvalsPage() {
  const [selected, setSelected] = useState("");
  const [editor, setEditor] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState("");
  const [receipt, setReceipt] = useState("");
  const [operationId, setOperationId] = useState("");
  const [observationOperation, setObservationOperation] = useState("");
  const [proposal, setProposal] = useState("");
  const [nativeTasks, setNativeTasks] = useState<unknown[]>([]);
  const [sampleId, setSampleId] = useState("");
  const [sampleEpoch, setSampleEpoch] = useState("1");
  const [studyEditor, setStudyEditor] = useState("");
  const [calibrationEditors, setCalibrationEditors] = useState<
    Record<string, string>
  >({});
  const state = useQuery({
    queryKey: ["evals-state"],
    queryFn: () => request("/state"),
    refetchInterval: 1500,
  });
  const detail = useQuery({
    queryKey: ["evals-experiment", selected],
    queryFn: () => request(`/experiments/${selected}?view=summary`),
    enabled: selected !== "",
    refetchInterval: 1500,
  });
  const snapshot = state.data === undefined ? undefined : record(state.data);
  const experiments = snapshot ? records(snapshot.experiments) : [];
  const experiment =
    detail.data === undefined ? undefined : record(detail.data);
  const candidates = experiment ? records(experiment.candidates) : [];
  const operations = snapshot ? records(snapshot.operations) : [];
  const observations = snapshot ? records(snapshot.observations) : [];
  const studies = snapshot ? records(snapshot.strategies) : [];
  const experiences = snapshot ? records(snapshot.experiences) : [];
  const plan = experiment ? record(experiment.plan) : {};
  const calibrationPanels = [
    {
      kind: "analyzer",
      label: "语义分析",
      definition: plan.semantic_analyzer,
      records: snapshot?.analyzer_calibrations,
    },
    {
      kind: "grader",
      label: "语义业务评分",
      definition: plan.semantic_grader,
      records: snapshot?.grader_calibrations,
    },
  ].flatMap((entry) => {
    if (entry.definition === undefined || entry.definition === null) return [];
    const definition = record(entry.definition);
    const value = entry.records
      ? record(entry.records)[text(definition.calibration_id)]
      : undefined;
    return [
      {
        ...entry,
        definition,
        result: value === undefined ? undefined : record(value),
      },
    ];
  });
  const terminal =
    experiment !== undefined &&
    [
      "completed",
      "cancelled",
      "cancel_pending_confirmation",
      "blocked",
      "interrupted",
    ].includes(text(experiment.status));

  function action(work: () => Promise<void>): void {
    setBusy(true);
    setMessage("");
    work()
      .then(() => state.refetch())
      .then(() => (selected ? detail.refetch() : undefined))
      .catch((error: unknown) => {
        setMessage(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        setBusy(false);
      });
  }

  function loadTemplate(kind: string): void {
    action(async () => {
      const template = await request(`/templates/${kind}`);
      setObservationOperation("");
      setEditor(JSON.stringify(template, null, 2));
    });
  }

  function create(): void {
    action(async () => {
      const parsed: unknown = JSON.parse(editor);
      const body = record(parsed);
      if (observationOperation) {
        const created = record(
          await request("/observations", {
            operation_id: observationOperation,
            plan: body.plan,
            compare_previous: body.compare_previous === true,
          })
        );
        setSelected(text(created.id));
        setMessage("已启动采用后观测，将关联实际生效版本。");
        setObservationOperation("");
        return;
      }
      const application = record(record(body.plan).application);
      const applications = snapshot ? record(snapshot.applications) : {};
      if (!(text(application.id) in applications))
        await request("/applications", application);
      const created = record(await request("/experiments", parsed));
      setSelected(text(created.id));
      setMessage("实验已准备，点击开始执行。");
    });
  }

  function control(command: string): void {
    action(async () => {
      await request(`/experiments/${selected}/control/${command}`, {});
    });
  }

  function adopt(candidateId: string): void {
    action(async () => {
      if (!experiment) return;
      const plan = record(experiment.plan);
      const application = record(plan.application);
      const result = record(
        await request("/operations", {
          experiment_id: selected,
          candidate_id: candidateId,
          expected_baseline: application.version,
          requested_version: version,
        })
      );
      setOperationId(text(result.id));
      setMessage("已生成采用请求。目标应用执行后提交实际生效回执。");
    });
  }

  function rollback(operation: Record<string, unknown>): void {
    action(async () => {
      const result = record(
        await request("/operations", {
          experiment_id: operation.experiment_id,
          candidate_id: operation.candidate_id,
          expected_baseline: operation.actual_version,
          requested_version: operation.expected_baseline,
          rollback: operation.id,
        })
      );
      setOperationId(text(result.id));
      setMessage("已生成回滚请求，等待执行方回执。");
    });
  }

  function sendReceipt(): void {
    action(async () => {
      const parsed: unknown = JSON.parse(receipt);
      await request(`/operations/${operationId}/receipt`, parsed);
      setMessage("已记录并核验回执。");
    });
  }

  function prepareObservation(operation: Record<string, unknown>): void {
    action(async () => {
      const original = record(
        await request(
          `/experiments/${text(operation.experiment_id)}?view=summary`
        )
      );
      const applications = snapshot ? record(snapshot.applications) : {};
      const current = record(applications[text(operation.application_id)]);
      setObservationOperation(text(operation.id));
      setEditor(
        JSON.stringify(
          {
            plan: {
              ...record(original.plan),
              application: current.declaration,
              cases: [],
            },
            proposals: [],
            use_gepa: false,
            compare_previous: true,
          },
          null,
          2
        )
      );
      setMessage("请在实验清单中填写新的业务任务与分区，再提交采用后观测。");
    });
  }

  function readNativeTasks(): void {
    action(async () => {
      const result = await request(`/experiments/${selected}/native`);
      setNativeTasks(Array.isArray(result) ? result : []);
    });
  }

  return (
    <main className={styles.page} translate="no">
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>EVALS / INSPECT AI</p>
          <h1>评测与持续改进</h1>
          <p>基线、证据、多个候选与独立验证，关联实际生效版本。</p>
        </div>
        <Link className="btn btn-outline-secondary" to="/">
          Inspect 日志
        </Link>
      </header>
      <p className={styles.notice}>
        本机单可信操作者 · 夹具实验会单独标记 · 未收到有效回执前保持原版
      </p>
      {(message || state.error || detail.error) && (
        <div role="status" className="alert alert-info">
          {message || state.error?.message || detail.error?.message}
        </div>
      )}
      <div className={styles.columns}>
        <section className={styles.panel}>
          <h2>实验</h2>
          {state.isPending && <p>正在读取工作目录…</p>}
          {!state.isPending && experiments.length === 0 && (
            <p>尚无实验。可载入夹具清单，或粘贴应用评测清单。</p>
          )}
          {experiments.map((item) => (
            <button
              key={text(item.id)}
              className={styles.experiment}
              aria-pressed={selected === item.id}
              onClick={() => {
                setSelected(text(item.id));
              }}
            >
              <strong>{text(item.application_id)}</strong>
              <span>
                {text(item.status)} / {text(item.stage)}
              </span>
              <small>{text(item.id)}</small>
            </button>
          ))}
          <hr />
          <h2>准备实验</h2>
          <div className={styles.actions}>
            <button
              disabled={busy}
              onClick={() => {
                loadTemplate("harness");
              }}
              className="btn btn-outline-secondary"
            >
              Harness 夹具
            </button>
            <button
              disabled={busy}
              onClick={() => {
                loadTemplate("rag");
              }}
              className="btn btn-outline-secondary"
            >
              RAG 夹具
            </button>
          </div>
          <label className={styles.label} htmlFor="evals-manifest">
            应用、冻结方案与候选 JSON
          </label>
          <textarea
            id="evals-manifest"
            value={editor}
            onChange={(event) => {
              setEditor(event.target.value);
            }}
            rows={10}
          />
          <button
            className="btn btn-primary mt-2"
            disabled={busy || editor === ""}
            onClick={create}
          >
            {observationOperation ? "提交采用后观测" : "准备实验"}
          </button>
        </section>
        <section className={styles.panel}>
          <h2>实验详情</h2>
          {!experiment && <p>选择实验后查看八维覆盖、候选差异与独立验证。</p>}
          {experiment && (
            <>
              <div className={styles.actions}>
                <strong>
                  {text(experiment.status)} / {text(experiment.stage)}
                </strong>
                {experiment.status === "draft" && (
                  <button
                    disabled={
                      busy ||
                      calibrationPanels.some(
                        (panel) => panel.result?.status !== "passed"
                      )
                    }
                    className="btn btn-primary"
                    onClick={() => {
                      action(async () => {
                        await request(`/experiments/${selected}/start`, {
                          fingerprint: experiment.fingerprint,
                        });
                      });
                    }}
                  >
                    开始执行
                  </button>
                )}
                <button
                  disabled={busy || terminal || experiment.status === "draft"}
                  onClick={() => {
                    control("pause");
                  }}
                  className="btn btn-outline-secondary"
                >
                  暂停
                </button>
                <button
                  disabled={
                    busy ||
                    terminal ||
                    !["paused", "pause_pending"].includes(
                      text(experiment.status)
                    )
                  }
                  onClick={() => {
                    control("resume");
                  }}
                  className="btn btn-outline-secondary"
                >
                  恢复
                </button>
                <button
                  disabled={busy || terminal}
                  onClick={() => {
                    control("cancel");
                  }}
                  className="btn btn-outline-danger"
                >
                  取消实验
                </button>
              </div>
              {experiment.reason !== undefined && (
                <p role="status">{text(experiment.reason)}</p>
              )}
              <p>
                证据类型：
                {text(record(record(experiment.plan).application).provenance)}
              </p>
              {calibrationPanels.map(({ kind, label, definition, result }) => {
                const editorId = `evals-${kind}-calibration`;
                const editorKey = `${selected}/${kind}`;
                const editor = calibrationEditors[editorKey] ?? "";
                return (
                  <section key={kind} aria-label={`${label}校准`}>
                    <h3>{label}校准</h3>
                    <p>
                      {text(definition.question)} ·{" "}
                      {text(definition.model_version)} · 校准状态：
                      {result ? text(result.status) : "未校准"}
                    </p>
                    <p>
                      使用独立标注的正例、负例和缺失证据，达到冻结门槛后才能开始实验。
                      {kind === "grader"
                        ? "通过校准的语义判定进入原生样本评分。"
                        : "模型原因解释保留为诊断假设。"}
                    </p>
                    {["draft", "blocked"].includes(text(experiment.status)) &&
                      records(experiment.runs).length === 0 &&
                      result?.status !== "passed" && (
                        <>
                          <label htmlFor={editorId}>
                            独立校准样本 JSON（cases
                            数组，含证据引用和预期标签）
                          </label>
                          <textarea
                            id={editorId}
                            rows={6}
                            value={editor}
                            onChange={(event) => {
                              const value = event.target.value;
                              setCalibrationEditors((previous) => ({
                                ...previous,
                                [editorKey]: value,
                              }));
                            }}
                          />
                          <button
                            className="btn btn-outline-primary mt-2"
                            disabled={busy || !editor}
                            onClick={() => {
                              action(async () => {
                                const parsed: unknown = JSON.parse(editor);
                                await request(
                                  `/experiments/${selected}/${kind}-calibration`,
                                  parsed
                                );
                                setMessage(
                                  `校准已入队，费用计入${kind === "grader" ? "评分" : "分析"}预算。`
                                );
                              });
                            }}
                          >
                            运行{label}校准
                          </button>
                        </>
                      )}
                    {result && (
                      <details>
                        <summary>校准结果与引用</summary>
                        <pre>{JSON.stringify(result, null, 2)}</pre>
                      </details>
                    )}
                  </section>
                );
              })}
              <h3>候选与独立决定</h3>
              {experiment.status === "draft" && (
                <div className={styles.actions}>
                  <button
                    className="btn btn-outline-primary"
                    disabled={busy}
                    onClick={() => {
                      action(async () => {
                        await request(
                          `/experiments/${selected}/configuration-candidates`,
                          {}
                        );
                      });
                    }}
                  >
                    生成声明范围内的配置候选
                  </button>
                  <details>
                    <summary>提交候选提案</summary>
                    <label htmlFor="evals-proposal">
                      改动、来源、原因假设、风险及预期基线
                    </label>
                    <textarea
                      id="evals-proposal"
                      rows={4}
                      value={proposal}
                      onChange={(e) => {
                        setProposal(e.target.value);
                      }}
                    />
                    <button
                      className="btn btn-outline-secondary"
                      disabled={busy || !proposal}
                      onClick={() => {
                        action(async () => {
                          const parsed: unknown = JSON.parse(proposal);
                          await request(
                            `/experiments/${selected}/candidates`,
                            parsed
                          );
                          setProposal("");
                        });
                      }}
                    >
                      登记提案
                    </button>
                  </details>
                </div>
              )}
              <label htmlFor="evals-version">请求生效版本</label>
              <input
                id="evals-version"
                value={version}
                onChange={(e) => {
                  setVersion(e.target.value);
                }}
                placeholder="例如 app-v2"
              />
              <div className={styles.tableWrap}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>来源 / 候选</th>
                      <th>配置差异</th>
                      <th>决定</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidates.map((candidate) => {
                      const reportValue = record(experiment.reports)[
                        text(candidate.id)
                      ];
                      const selectionValue =
                        experiment.selection_reports === undefined
                          ? undefined
                          : record(experiment.selection_reports)[
                              text(candidate.id)
                            ];
                      const report =
                        reportValue === undefined
                          ? undefined
                          : record(reportValue);
                      const decision = report
                        ? text(report.decision)
                        : selectionValue === undefined
                          ? "待验证"
                          : `选优：${text(record(selectionValue).decision)}`;
                      return (
                        <tr key={text(candidate.id)}>
                          <td>
                            {text(candidate.source)}
                            <br />
                            <small>{text(candidate.id)}</small>
                          </td>
                          <td>
                            <code>
                              {JSON.stringify(
                                candidate.changes ?? candidate.config
                              )}
                            </code>
                            <br />
                            {text(candidate.hypothesis)}
                          </td>
                          <td>
                            {decision}
                            {report?.decision === "recommend" && (
                              <button
                                disabled={busy || version === ""}
                                onClick={() => {
                                  adopt(text(candidate.id));
                                }}
                                className="btn btn-sm btn-primary d-block"
                              >
                                请求采用
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <h3>八维覆盖与原始证据</h3>
              <RunEvidence runs={experiment.runs} />
              <details>
                <summary>Scout 发现与原因假设</summary>
                <pre>{JSON.stringify(experiment.findings, null, 2)}</pre>
              </details>
              <details>
                <summary>独立验证报告与退化样本</summary>
                <pre>{JSON.stringify(experiment.reports, null, 2)}</pre>
              </details>
              <details>
                <summary>预算、未知费用与在途预留</summary>
                <pre>{JSON.stringify(experiment.ledger, null, 2)}</pre>
              </details>
              {!terminal && experiment.status !== "draft" && (
                <details>
                  <summary>活动评测与样本恢复</summary>
                  <button
                    className="btn btn-outline-secondary"
                    disabled={busy}
                    onClick={readNativeTasks}
                  >
                    读取所属运行
                  </button>
                  <pre>{JSON.stringify(nativeTasks, null, 2)}</pre>
                  <label htmlFor="evals-sample">样本标识</label>
                  <input
                    id="evals-sample"
                    value={sampleId}
                    onChange={(e) => {
                      setSampleId(e.target.value);
                    }}
                  />
                  <label htmlFor="evals-epoch">重复轮次</label>
                  <input
                    id="evals-epoch"
                    type="number"
                    min="1"
                    value={sampleEpoch}
                    onChange={(e) => {
                      setSampleEpoch(e.target.value);
                    }}
                  />
                  <button
                    className="btn btn-outline-secondary"
                    disabled={busy || !sampleId}
                    onClick={() => {
                      action(async () => {
                        await request(
                          `/experiments/${selected}/samples/requeue`,
                          { sample_id: sampleId, epoch: Number(sampleEpoch) }
                        );
                      });
                    }}
                  >
                    将样本重新入队
                  </button>
                </details>
              )}
            </>
          )}
        </section>
      </div>
      <section className={styles.panel}>
        <h2>采用与回滚</h2>
        {operations.length === 0 && (
          <p>尚无版本操作。只有独立验证通过的候选可以请求采用。</p>
        )}
        {operations.map((operation) => (
          <details key={text(operation.id)}>
            <summary>
              {text(operation.id)} · {text(operation.status)} ·{" "}
              {text(operation.requested_version)}
            </summary>
            <pre>{JSON.stringify(operation, null, 2)}</pre>
            <button
              className="btn btn-outline-secondary"
              onClick={() => {
                setOperationId(text(operation.id));
              }}
            >
              选择回执操作
            </button>
            {operation.status === "applied" && !operation.rollback_of && (
              <button
                disabled={busy}
                className="btn btn-outline-danger"
                onClick={() => {
                  rollback(operation);
                }}
              >
                请求回滚
              </button>
            )}
            {operation.status === "applied" && !operation.rollback_of && (
              <button
                disabled={busy}
                className="btn btn-outline-primary"
                onClick={() => {
                  prepareObservation(operation);
                }}
              >
                准备采用后评测
              </button>
            )}
          </details>
        ))}
        <label className={styles.label} htmlFor="evals-operation">
          操作标识
        </label>
        <input
          id="evals-operation"
          value={operationId}
          onChange={(e) => {
            setOperationId(e.target.value);
          }}
        />
        <label className={styles.label} htmlFor="evals-receipt">
          执行方实际生效回执 JSON
        </label>
        <textarea
          id="evals-receipt"
          rows={5}
          value={receipt}
          onChange={(e) => {
            setReceipt(e.target.value);
          }}
        />
        <button
          disabled={busy || !operationId || !receipt}
          className="btn btn-primary mt-2"
          onClick={sendReceipt}
        >
          核验回执
        </button>
      </section>
      <section className={styles.panel}>
        <h2>采用后观测</h2>
        {observations.length === 0 && (
          <p>收到实际生效回执并运行新任务后，在此查看结果。</p>
        )}
        {observations.map((item) => (
          <details key={text(item.id)}>
            <summary>
              {text(item.application_id)} · {text(item.version)} ·{" "}
              {text(item.provenance)}
            </summary>
            <p>
              {text(item.started_at)} → {text(item.completed_at)}
            </p>
            <p>
              安全与业务硬约束失败：{String(item.hard_failures)}。
              {text(item.rollback_basis)}
            </p>
            <p>对照依据：{text(item.comparison_basis)}</p>
            <p>
              配置收益结论：
              {item.validation === undefined
                ? "历史描述性记录，未作独立确认"
                : text(record(item.validation).decision)}
            </p>
            <pre>{JSON.stringify(item.summary, null, 2)}</pre>
            <details>
              <summary>新旧配置对照与不确定性</summary>
              <pre>{JSON.stringify(item.comparisons, null, 2)}</pre>
            </details>
          </details>
        ))}
      </section>
      <section className={styles.panel}>
        <h2>平台策略对照与经验</h2>
        <p>
          后续默认策略（仅在验证范围内生效）：
          {snapshot?.default_strategy === undefined
            ? "builtin-search-v1"
            : text(record(record(snapshot.default_strategy).policy).version)}
        </p>
        <p>
          旧策略、简单规则与新策略在冻结任务和预算下比较；证据不足时保留旧策略。
        </p>
        <details>
          <summary>创建策略对照</summary>
          <label htmlFor="evals-study">策略版本、三个实验臂与每臂预算</label>
          <textarea
            id="evals-study"
            rows={4}
            value={studyEditor}
            onChange={(e) => {
              setStudyEditor(e.target.value);
            }}
          />
          <button
            className="btn btn-outline-primary"
            disabled={busy || !studyEditor}
            onClick={() => {
              action(async () => {
                const parsed: unknown = JSON.parse(studyEditor);
                await request("/strategies", parsed);
                setStudyEditor("");
              });
            }}
          >
            冻结策略对照
          </button>
        </details>
        {studies.map((item) => (
          <details key={text(item.id)}>
            <summary>
              {text(item.version)} · {text(item.decision)}
            </summary>
            <pre>{JSON.stringify(item, null, 2)}</pre>
            <button
              disabled={busy}
              className="btn btn-outline-secondary"
              onClick={() => {
                action(async () => {
                  await request(`/strategies/${text(item.id)}/evaluate`, {});
                });
              }}
            >
              更新对照结论
            </button>
            {item.decision === "recommend" &&
              item.promotion_eligible === true && (
                <button
                  disabled={busy}
                  className="btn btn-outline-primary"
                  onClick={() => {
                    action(async () => {
                      await request(`/strategies/${text(item.id)}/promote`, {
                        expected_default_version: text(
                          record(item.default_baseline).version
                        ),
                      });
                    });
                  }}
                >
                  将策略设为后续默认
                </button>
              )}
          </details>
        ))}
        {experiences.map((item) => (
          <details key={`${text(item.id)}:${text(item.version)}`}>
            <summary>
              {text(item.id)} / {text(item.version)} ·{" "}
              {item.enabled ? "已启用" : "已停用"}
            </summary>
            <p>{text(item.text)}</p>
            <p>
              应用：{text(item.application_id)}；有效期至{" "}
              {text(item.expires_at)}
            </p>
            <button
              disabled={busy || !item.enabled}
              className="btn btn-outline-secondary"
              onClick={() => {
                action(async () => {
                  await request(
                    `/experiences/${text(item.id)}/${text(item.version)}/disable`,
                    {}
                  );
                });
              }}
            >
              停用经验
            </button>
          </details>
        ))}
      </section>
    </main>
  );
}
