export function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Expected a JSON object");
  }
  return Object.fromEntries(Object.entries(value));
}

export function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function records(value: unknown): Record<string, unknown>[] {
  return Object.values(record(value)).map(record);
}

export function percentage(value: unknown): string {
  return typeof value === "number"
    ? `${(value * 100).toFixed(1)}%`
    : "不可计算";
}

export const dimensionNames: Record<string, string> = {
  result: "结果",
  retrieval: "检索",
  evidence: "证据",
  process: "过程",
  stability: "稳定性",
  recovery: "异常处理",
  cost: "成本效率",
  security: "安全权限",
};
