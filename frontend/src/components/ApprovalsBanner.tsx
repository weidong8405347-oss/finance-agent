import { useEffect, useState } from "react";
import { api, ApprovalRow } from "../api";

// milestone 档（D3）：高成本/高风险操作的审批对话框
export default function ApprovalsBanner() {
  const [pending, setPending] = useState<ApprovalRow[]>([]);

  useEffect(() => {
    const tick = () => api.pendingApprovals().then(setPending).catch(() => {});
    tick();
    const timer = setInterval(tick, 2000);
    return () => clearInterval(timer);
  }, []);

  if (pending.length === 0) return null;

  return (
    <div className="fixed inset-x-0 top-0 z-50 flex justify-center p-3">
      <div className="w-full max-w-xl rounded-lg border border-amber-300 bg-amber-50 shadow-lg">
        {pending.map((a) => (
          <div key={a.approval_id} className="flex items-center gap-3 px-4 py-3">
            <span className="text-amber-600">⚠</span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium">操作待审批（milestone 档）</div>
              <pre className="mt-1 overflow-auto text-xs text-neutral-600">
                {JSON.stringify(a.detail, null, 2)}
              </pre>
            </div>
            <button
              onClick={() => api.decideApproval(a.approval_id, true).then(() => setPending(pending.filter((x) => x.approval_id !== a.approval_id)))}
              className="rounded bg-neutral-900 px-3 py-1.5 text-xs text-white"
            >
              批准
            </button>
            <button
              onClick={() => api.decideApproval(a.approval_id, false).then(() => setPending(pending.filter((x) => x.approval_id !== a.approval_id)))}
              className="rounded border border-neutral-300 px-3 py-1.5 text-xs"
            >
              拒绝
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
