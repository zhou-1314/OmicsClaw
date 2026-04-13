import React, { useEffect, useState } from 'react';
import {
  Activity,
  Database,
  FileText,
  RefreshCw,
  Search,
  ShieldAlert,
  Workflow,
} from 'lucide-react';
import clsx from 'clsx';

import {
  getKgHealth,
  getKgPage,
  getKgStatus,
  getKgWorkspace,
  listKgPages,
  searchKg,
  setKgWorkspace,
} from '../../lib/api';

const PAGE_TYPES = [
  { value: 'sources', label: 'Sources' },
  { value: 'hypotheses', label: 'Hypotheses' },
  { value: 'concepts', label: 'Concepts' },
  { value: 'methods', label: 'Methods' },
];

const HYPOTHESIS_STATUSES = [
  { value: '', label: 'All Statuses' },
  { value: 'draft', label: 'Draft' },
  { value: 'testing', label: 'Testing' },
  { value: 'validated', label: 'Validated' },
  { value: 'refuted', label: 'Refuted' },
];

function MetricCard({ label, value, tone = 'slate' }) {
  const toneClass = {
    slate: 'border-slate-800/60 bg-slate-900/40 text-slate-200',
    emerald: 'border-emerald-500/20 bg-emerald-950/20 text-emerald-300',
    amber: 'border-amber-500/20 bg-amber-950/20 text-amber-300',
  }[tone];

  return (
    <div className={clsx('rounded-xl border p-4 backdrop-blur-sm', toneClass)}>
      <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">{label}</div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
    </div>
  );
}

export default function KgPage() {
  const [workspaceInput, setWorkspaceInput] = useState(getKgWorkspace());
  const [connectedWorkspace, setConnectedWorkspace] = useState(getKgWorkspace());
  const [pageType, setPageType] = useState('sources');
  const [query, setQuery] = useState('');
  const [hypothesisStatus, setHypothesisStatus] = useState('');
  const [status, setStatus] = useState(null);
  const [health, setHealth] = useState(null);
  const [items, setItems] = useState([]);
  const [itemsTotal, setItemsTotal] = useState(0);
  const [selectedPage, setSelectedPage] = useState(null);
  const [loadingOverview, setLoadingOverview] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);
  const [pageLoading, setPageLoading] = useState(false);
  const [error, setError] = useState('');

  const ensureWorkspace = () => {
    const normalized = setKgWorkspace(workspaceInput);
    setWorkspaceInput(normalized);
    setConnectedWorkspace(normalized);
    return normalized;
  };

  const loadOverview = async () => {
    const workspace = ensureWorkspace();
    if (!workspace) {
      setError('请输入项目根目录或 .omicsclaw/knowledge 路径。');
      return;
    }

    setLoadingOverview(true);
    setError('');
    try {
      const [statusData, healthData] = await Promise.all([
        getKgStatus(),
        getKgHealth(),
      ]);
      setStatus(statusData);
      setHealth(healthData);
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'KG backend unavailable');
    } finally {
      setLoadingOverview(false);
    }
  };

  const loadPages = async (nextType = pageType) => {
    const workspace = ensureWorkspace();
    if (!workspace) {
      setItems([]);
      setItemsTotal(0);
      return;
    }

    setLoadingItems(true);
    setError('');
    try {
      const payload = await listKgPages(nextType, {
        status: nextType === 'hypotheses' ? hypothesisStatus || undefined : undefined,
        limit: 50,
      });
      setItems(
        (payload.pages || []).map((item) => ({
          ...item,
          page_type: nextType,
        }))
      );
      setItemsTotal(payload.total || 0);
    } catch (err) {
      setItems([]);
      setItemsTotal(0);
      setError(err.response?.data?.detail || err.message || 'Failed to load KG pages');
    } finally {
      setLoadingItems(false);
    }
  };

  const openPage = async (nextType, slug) => {
    setPageLoading(true);
    try {
      const payload = await getKgPage(nextType, slug);
      setSelectedPage(payload);
      setError('');
    } catch (err) {
      setSelectedPage(null);
      setError(err.response?.data?.detail || err.message || 'Failed to open KG page');
    } finally {
      setPageLoading(false);
    }
  };

  const handleSearch = async (event) => {
    event.preventDefault();
    const workspace = ensureWorkspace();
    if (!workspace) {
      setError('请输入 KG workspace。');
      return;
    }
    if (!query.trim()) {
      await loadPages(pageType);
      return;
    }

    setLoadingItems(true);
    setError('');
    try {
      const payload = await searchKg(query.trim(), {
        pageType,
        status: pageType === 'hypotheses' ? hypothesisStatus || undefined : undefined,
        limit: 20,
      });
      setItems(payload.hits || []);
      setItemsTotal(payload.total || 0);
    } catch (err) {
      setItems([]);
      setItemsTotal(0);
      setError(err.response?.data?.detail || err.message || 'KG search failed');
    } finally {
      setLoadingItems(false);
    }
  };

  useEffect(() => {
    const workspace = getKgWorkspace();
    if (!workspace) return;
    setWorkspaceInput(workspace);
    setConnectedWorkspace(workspace);
    void loadOverview();
    void loadPages(pageType);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!connectedWorkspace) return;
    void loadPages(pageType);
  }, [pageType, hypothesisStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex h-full bg-slate-950 text-slate-300 overflow-hidden relative">
      <div className="absolute top-0 left-0 right-0 h-80 bg-gradient-to-b from-cyan-900/10 via-slate-950 to-transparent pointer-events-none" />
      <div className="w-full max-w-7xl mx-auto p-8 relative z-10 flex flex-col min-h-0">
        <header className="border-b border-slate-800/50 pb-6 mb-6">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-11 h-11 rounded-2xl bg-cyan-500/10 border border-cyan-500/20 flex items-center justify-center">
              <Workflow className="w-5 h-5 text-cyan-300" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white tracking-tight">KG Explorer</h1>
              <p className="text-sm text-slate-400">
                通过 app-server 统一查看 OmicsClaw-KG 的状态、检索结果和页面内容。
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-[1fr_auto_auto] gap-3">
            <input
              value={workspaceInput}
              onChange={(e) => setWorkspaceInput(e.target.value)}
              placeholder="/path/to/project 或 /path/to/project/.omicsclaw/knowledge"
              className="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-3 text-sm text-slate-200 outline-none focus:border-cyan-400/40"
            />
            <button
              onClick={() => {
                void loadOverview();
                void loadPages(pageType);
              }}
              className="rounded-xl border border-cyan-500/30 bg-cyan-500/10 px-5 py-3 text-sm font-semibold text-cyan-300 hover:bg-cyan-500/20"
            >
              Connect KG
            </button>
            <button
              onClick={() => {
                void loadOverview();
                void loadPages(pageType);
              }}
              className="rounded-xl border border-slate-700 bg-slate-900/70 px-4 py-3 text-sm text-slate-300 hover:border-slate-600"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>

          <div className="mt-3 text-xs text-slate-500">
            当前 workspace: {connectedWorkspace || '未配置'}
          </div>
        </header>

        {error && (
          <div className="mb-6 rounded-xl border border-rose-900/40 bg-rose-950/20 px-4 py-3 text-sm text-rose-300 flex items-start gap-3">
            <ShieldAlert className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <div>{error}</div>
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
          <MetricCard
            label="Wiki Pages"
            value={loadingOverview ? '...' : (status?.wiki_total ?? 0)}
            tone="slate"
          />
          <MetricCard
            label="Graph Nodes"
            value={loadingOverview ? '...' : (status?.graph_nodes ?? 0)}
            tone="emerald"
          />
          <MetricCard
            label="Health Errors"
            value={loadingOverview ? '...' : (health?.n_errors ?? 0)}
            tone={health?.n_errors ? 'amber' : 'emerald'}
          />
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-6 min-h-0 flex-1">
          <section className="rounded-2xl border border-slate-800/60 bg-slate-900/40 backdrop-blur-sm flex flex-col min-h-0">
            <div className="p-4 border-b border-slate-800/60">
              <div className="flex flex-wrap gap-2 mb-4">
                {PAGE_TYPES.map((item) => (
                  <button
                    key={item.value}
                    onClick={() => setPageType(item.value)}
                    className={clsx(
                      'rounded-full px-3 py-1.5 text-xs font-semibold border transition-colors',
                      pageType === item.value
                        ? 'border-cyan-400/30 bg-cyan-500/10 text-cyan-300'
                        : 'border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-200'
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>

              <form onSubmit={handleSearch} className="space-y-3">
                <div className="relative">
                  <Search className="w-4 h-4 absolute left-3 top-3.5 text-slate-500" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder={`Search ${pageType}`}
                    className="w-full rounded-xl border border-slate-800 bg-slate-950/70 pl-10 pr-4 py-3 text-sm text-slate-200 outline-none focus:border-cyan-400/40"
                  />
                </div>
                {pageType === 'hypotheses' && (
                  <select
                    value={hypothesisStatus}
                    onChange={(e) => setHypothesisStatus(e.target.value)}
                    className="w-full rounded-xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm text-slate-200 outline-none focus:border-cyan-400/40"
                  >
                    {HYPOTHESIS_STATUSES.map((item) => (
                      <option key={item.value || 'all'} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                )}
                <button
                  type="submit"
                  className="w-full rounded-xl border border-cyan-500/30 bg-cyan-500/10 px-4 py-3 text-sm font-semibold text-cyan-300 hover:bg-cyan-500/20"
                >
                  Search / Refresh
                </button>
              </form>
            </div>

            <div className="px-4 pt-3 text-xs uppercase tracking-[0.2em] text-slate-500">
              Result Set · {itemsTotal}
            </div>

            <div className="p-4 overflow-y-auto custom-scrollbar flex-1 space-y-3">
              {loadingItems ? (
                <div className="text-sm text-slate-500 flex items-center gap-3">
                  <Activity className="w-4 h-4 animate-pulse" />
                  Loading KG results...
                </div>
              ) : items.length > 0 ? (
                items.map((item) => (
                  <button
                    key={`${item.page_type || pageType}:${item.slug}`}
                    onClick={() => void openPage(item.page_type || pageType, item.slug)}
                    className="w-full text-left rounded-xl border border-slate-800/60 bg-slate-950/40 p-4 hover:border-cyan-500/30 hover:bg-slate-900/80 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-slate-100 truncate">
                          {item.title || item.slug}
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          {(item.page_type || pageType)}/{item.slug}
                        </div>
                      </div>
                      {typeof item.score === 'number' && (
                        <span className="text-[11px] text-cyan-300">
                          {item.score.toFixed(2)}
                        </span>
                      )}
                    </div>
                    {(item.status || item.state) && (
                      <div className="mt-3 flex gap-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                        {item.status && <span>{item.status}</span>}
                        {item.state && <span>{item.state}</span>}
                      </div>
                    )}
                  </button>
                ))
              ) : (
                <div className="rounded-xl border border-dashed border-slate-800 p-6 text-sm text-slate-500">
                  当前没有可显示结果。确认 KG HTTP 服务已启动，且 workspace 指向正确。
                </div>
              )}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-800/60 bg-slate-900/40 backdrop-blur-sm min-h-0 overflow-hidden flex flex-col">
            <div className="p-5 border-b border-slate-800/60 flex items-center gap-3">
              <FileText className="w-4 h-4 text-cyan-300" />
              <div>
                <div className="text-sm font-semibold text-slate-100">Page Detail</div>
                <div className="text-xs text-slate-500">
                  选择左侧条目查看 frontmatter 与正文
                </div>
              </div>
            </div>

            <div className="p-5 overflow-y-auto custom-scrollbar flex-1">
              {pageLoading ? (
                <div className="text-sm text-slate-500">Loading page...</div>
              ) : selectedPage ? (
                <div className="space-y-6">
                  <div>
                    <div className="text-xs uppercase tracking-[0.18em] text-slate-500 mb-2">
                      Metadata
                    </div>
                    <div className="rounded-xl border border-slate-800/60 bg-slate-950/50 p-4">
                      <div className="text-lg font-semibold text-white mb-2">
                        {selectedPage.frontmatter?.title || selectedPage.slug}
                      </div>
                      <div className="text-sm text-slate-500 mb-4">
                        {selectedPage.page_type}/{selectedPage.slug}
                      </div>
                      <pre className="text-xs text-slate-300 whitespace-pre-wrap break-words">
                        {JSON.stringify(selectedPage.frontmatter, null, 2)}
                      </pre>
                    </div>
                  </div>

                  <div>
                    <div className="text-xs uppercase tracking-[0.18em] text-slate-500 mb-2">
                      Body
                    </div>
                    <div className="rounded-xl border border-slate-800/60 bg-slate-950/50 p-4">
                      <pre className="text-sm leading-6 text-slate-200 whitespace-pre-wrap break-words">
                        {selectedPage.body || 'No body content'}
                      </pre>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-center text-slate-500 gap-4">
                  <Database className="w-8 h-8 text-slate-700" />
                  <div>
                    <div className="text-sm text-slate-400">KG page viewer is ready</div>
                    <div className="text-xs mt-2 max-w-md">
                      开发环境中 <code>/kg</code> 默认会转到 `oc app-server` (`127.0.0.1:8765`)。
                      启动前请确保 `omicsclaw_kg` 可导入；源码联调时可设置
                      `OMICSCLAW_KG_SOURCE_DIR`，然后再输入 workspace。
                    </div>
                  </div>
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
