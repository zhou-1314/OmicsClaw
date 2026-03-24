import React, { useEffect, useState, useCallback } from 'react';
import {
  Trash2, Sparkles, AlertTriangle, RefreshCw,
  ChevronDown, ChevronUp, ArrowRight, Unlink, Archive, CheckSquare, Square, Minus
} from 'lucide-react';
import { format } from 'date-fns';
import DiffViewer from '../../components/DiffViewer';
import { api } from '../../lib/api';

export default function MaintenancePage() {
  const [orphans, setOrphans] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Expand / detail
  const [expandedId, setExpandedId] = useState(null);
  const [detailData, setDetailData] = useState({});
  const [detailLoading, setDetailLoading] = useState(null);

  // Multi-select
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  useEffect(() => {
    loadOrphans();
  }, []);

  const loadOrphans = async () => {
    setLoading(true);
    setError(null);
    setSelectedIds(new Set());
    try {
      const res = await api.get('/maintenance/orphans');
      setOrphans(res.data);
    } catch (err) {
      setError("Failed to load orphans: " + (err.response?.data?.detail || err.message));
    } finally {
      setLoading(false);
    }
  };

  // Toggle single checkbox
  const toggleSelect = useCallback((id, e) => {
    e.stopPropagation();
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Select/deselect all in a category
  const toggleSelectAll = useCallback((items) => {
    const ids = items.map(i => i.id);
    setSelectedIds(prev => {
      const next = new Set(prev);
      const allSelected = ids.every(id => next.has(id));
      if (allSelected) {
        ids.forEach(id => next.delete(id));
      } else {
        ids.forEach(id => next.add(id));
      }
      return next;
    });
  }, []);

  // Batch delete
  const handleBatchDelete = async () => {
    const count = selectedIds.size;
    if (count === 0) return;
    if (!confirm(`Permanently delete ${count} memories? This cannot be undone.`)) return;

    setBatchDeleting(true);
    const toDelete = [...selectedIds];
    let failed = [];

    for (const id of toDelete) {
      try {
        await api.delete(`/maintenance/orphan/${id}`);
      } catch {
        failed.push(id);
      }
    }

    // Remove successfully deleted from list
    const failedSet = new Set(failed);
    setOrphans(prev => prev.filter(item => !toDelete.includes(item.id) || failedSet.has(item.id)));
    setSelectedIds(new Set(failed));

    if (expandedId && toDelete.includes(expandedId) && !failedSet.has(expandedId)) {
      setExpandedId(null);
    }

    if (failed.length > 0) {
      alert(`${failed.length} of ${count} deletions failed. Failed IDs: ${failed.join(', ')}`);
    }

    setBatchDeleting(false);
  };

  // Expand card
  const handleExpand = async (id) => {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);

    if (!detailData[id]) {
      setDetailLoading(id);
      try {
        const res = await api.get(`/maintenance/orphan/${id}`);
        setDetailData(prev => ({ ...prev, [id]: res.data }));
      } catch (err) {
        setDetailData(prev => ({ ...prev, [id]: { error: err.response?.data?.detail || err.message } }));
      } finally {
        setDetailLoading(null);
      }
    }
  };

  const deprecated = orphans.filter(o => o.category === 'deprecated');
  const orphaned = orphans.filter(o => o.category === 'orphaned');

  const renderCard = (item) => {
    const isExpanded = expandedId === item.id;
    const detail = detailData[item.id];
    const isLoadingDetail = detailLoading === item.id;
    const isChecked = selectedIds.has(item.id);
    let snippet = item.content_snippet;

    return (
      <div key={item.id} className="group relative glass-card">
        {/* Clickable Card Header */}
        <div
          className="flex items-start gap-3 p-4 cursor-pointer select-none"
          onClick={() => handleExpand(item.id)}
        >
          {/* Checkbox */}
          <button
            onClick={(e) => toggleSelect(item.id, e)}
            className="mt-0.5 flex-shrink-0 p-0.5 rounded transition-colors hover:bg-slate-700/30"
          >
            {isChecked ? (
              <CheckSquare size={18} className="text-indigo-400" />
            ) : (
              <Square size={18} className="text-slate-600 group-hover:text-slate-500" />
            )}
          </button>

          {/* Content area */}
          <div className="flex-1 min-w-0">
            {/* Top row: badges + time */}
            <div className="flex items-center gap-2 flex-wrap mb-1.5">
              <span className="text-[11px] font-mono text-slate-400 bg-slate-800/80 px-1.5 py-0.5 rounded">
                #{item.id}
              </span>
              {item.category === 'deprecated' ? (
                <span className="text-[10px] font-mono text-amber-300 bg-amber-900/40 px-1.5 py-0.5 rounded flex items-center gap-1">
                  <Archive size={9} /> deprecated
                </span>
              ) : (
                <span className="text-[10px] font-mono text-rose-300 bg-rose-900/40 px-1.5 py-0.5 rounded flex items-center gap-1">
                  <Unlink size={9} /> orphaned
                </span>
              )}
              {item.migrated_to && (
                <span className="text-[10px] font-mono text-indigo-300 bg-indigo-900/30 px-1.5 py-0.5 rounded">
                  → #{item.migrated_to}
                </span>
              )}
              <span className="text-[11px] text-slate-500">
                {item.created_at ? format(new Date(item.created_at), 'yyyy-MM-dd HH:mm') : 'Unknown'}
              </span>
            </div>

            {/* Migration target paths */}
            {item.migration_target && item.migration_target.paths.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap mb-2">
                <ArrowRight size={12} className="text-indigo-400/70 flex-shrink-0" />
                {item.migration_target.paths.map((p, i) => (
                  <span key={i} className="text-[11px] font-mono text-indigo-300/90 bg-indigo-900/25 px-1.5 py-0.5 rounded border border-indigo-800/30">
                    {p}
                  </span>
                ))}
              </div>
            )}
            {item.migration_target && item.migration_target.paths.length === 0 && (
              <div className="flex items-center gap-1.5 mb-2">
                <ArrowRight size={12} className="text-slate-500 flex-shrink-0" />
                <span className="text-[11px] text-slate-500 italic">
                  target #{item.migration_target.id} also has no paths
                </span>
              </div>
            )}

            {/* Content snippet */}
            <div className="bg-slate-900/60 rounded p-2.5 text-[12px] text-slate-400 font-mono leading-relaxed line-clamp-3">
              {snippet}
            </div>
          </div>

          {/* Expand indicator */}
          <div className="mt-1 flex-shrink-0 text-slate-500">
            {isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </div>
        </div>

        {/* Expanded Detail */}
        {isExpanded && (
          <div className="border-t border-white/5 p-5 bg-black/20 rounded-b-2xl backdrop-blur-md">
            {isLoadingDetail ? (
              <div className="flex items-center gap-3 text-slate-500 py-4">
                <div className="w-4 h-4 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin"></div>
                <span className="text-xs">Loading full content...</span>
              </div>
            ) : detail?.error ? (
              <div className="text-rose-400 text-xs py-2">Error: {detail.error}</div>
            ) : detail ? (
              <div className="space-y-4">
                {(() => {
                  let content = detail.content;
                  let migrationTargetContent = detail.migration_target?.content;
                  
                  return (
                    <>
                      {/* Full content */}
                      <div>
                  <h4 className="text-[11px] uppercase tracking-widest text-slate-500 mb-2 font-semibold">
                    {detail.migration_target ? 'Old Version (This Memory)' : 'Full Content'}
                  </h4>
                  <div className="bg-[#060610] rounded p-4 border border-slate-800/60 text-[12px] text-slate-300 font-mono leading-relaxed whitespace-pre-wrap max-h-64 overflow-y-auto custom-scrollbar">
                    {content}
                  </div>
                </div>

                {/* Diff with migration target */}
                {detail.migration_target && (
                  <div>
                    <h4 className="text-[11px] uppercase tracking-widest text-slate-500 mb-2 font-semibold flex items-center gap-2">
                      <span>Diff: #{item.id} → #{detail.migration_target.id}</span>
                      {detail.migration_target.paths.length > 0 && (
                        <span className="text-indigo-400/70 normal-case tracking-normal font-normal">
                          ({detail.migration_target.paths[0]})
                        </span>
                      )}
                    </h4>
                    <div className="bg-[#060610] rounded border border-slate-800/60 p-4 max-h-96 overflow-y-auto custom-scrollbar">
                      <DiffViewer
                        oldText={content}
                        newText={migrationTargetContent}
                      />
                    </div>
                  </div>
                )}
                    </>
                  );
                })()}
              </div>
            ) : null}
          </div>
        )}
      </div>
    );
  };

  // Section header with select-all checkbox
  const renderSectionHeader = (icon, label, color, items) => {
    const allSelected = items.length > 0 && items.every(i => selectedIds.has(i.id));
    const someSelected = items.some(i => selectedIds.has(i.id));

    return (
      <div className="flex items-center gap-3 mb-4">
        <button
          onClick={() => toggleSelectAll(items)}
          className="p-0.5 rounded transition-colors hover:bg-slate-700/30"
          title={allSelected ? "Deselect all" : "Select all"}
        >
          {allSelected ? (
            <CheckSquare size={16} className={color} />
          ) : someSelected ? (
            <Minus size={16} className={color} />
          ) : (
            <Square size={16} className="text-slate-600" />
          )}
        </button>
        {icon}
        <h3 className={`text-xs font-bold uppercase tracking-widest ${color}`}>
          {label}
        </h3>
        <span className="text-[11px] text-slate-500 bg-slate-800/80 px-2 py-0.5 rounded-full">
          {items.length}
        </span>
      </div>
    );
  };

  return (
    <div className="flex h-full bg-transparent text-slate-200 font-sans overflow-hidden">
      {/* Sidebar */}
      <div className="w-72 flex-shrink-0 glass-panel border-r border-white/5 flex flex-col p-6 relative z-20 shadow-[10px_0_30px_rgba(0,0,0,0.5)]">
        <div className="mb-8">
          <div className="w-12 h-12 bg-amber-950/30 rounded-xl flex items-center justify-center border border-amber-800/30 mb-4 shadow-[0_0_20px_rgba(245,158,11,0.1)]">
            <Sparkles className="text-amber-400" size={24} />
          </div>
          <h1 className="text-xl font-bold text-slate-100 mb-2">Brain Cleanup</h1>
          <p className="text-[12px] text-slate-400 leading-relaxed">
            Find and clean up orphan memories — deprecated versions from updates
            and unreachable memories from path deletions.
          </p>
        </div>

        <div className="space-y-3 mt-auto">
          <div className="bg-slate-800/40 rounded-lg p-4 border border-slate-700/40">
            <div className="text-slate-400 text-xs uppercase font-bold tracking-wider mb-1">Deprecated</div>
            <div className="text-3xl font-mono text-amber-400">{deprecated.length}</div>
            <div className="text-slate-500 text-[11px] mt-1">old versions from updates</div>
          </div>
          <div className="bg-slate-800/40 rounded-lg p-4 border border-slate-700/40">
            <div className="text-slate-400 text-xs uppercase font-bold tracking-wider mb-1">Orphaned</div>
            <div className="text-3xl font-mono text-rose-400">{orphaned.length}</div>
            <div className="text-slate-500 text-[11px] mt-1">unreachable (no paths)</div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col min-w-0 bg-transparent relative overflow-hidden z-10">
        {/* Header with batch actions */}
        <div className="h-14 flex items-center justify-between px-8 border-b border-white/5 bg-slate-900/30 backdrop-blur-xl sticky top-0 z-10 shadow-lg">
          <h2 className="text-sm font-bold text-slate-300 uppercase tracking-widest flex items-center gap-2">
            <Trash2 size={14} /> Orphan Memories
          </h2>
          <div className="flex items-center gap-2">
            {selectedIds.size > 0 && (
              <button
                onClick={handleBatchDelete}
                disabled={batchDeleting}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-rose-900/40 text-rose-300 hover:bg-rose-900/60 border border-rose-800/40 transition-colors disabled:opacity-50"
              >
                {batchDeleting ? (
                  <div className="w-3 h-3 border-2 border-rose-400/30 border-t-rose-400 rounded-full animate-spin"></div>
                ) : (
                  <Trash2 size={13} />
                )}
                Delete {selectedIds.size} selected
              </button>
            )}
            <button
              onClick={loadOrphans}
              className="p-2 text-slate-400 hover:text-indigo-400 hover:bg-slate-700/40 rounded-full transition-all"
              title="Refresh"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-8 custom-scrollbar">
          {loading ? (
            <div className="flex flex-col items-center justify-center h-64 text-slate-500 gap-4">
              <div className="w-6 h-6 border-2 border-amber-500/30 border-t-amber-500 rounded-full animate-spin"></div>
              <span className="text-xs tracking-widest uppercase">Scanning for orphans...</span>
            </div>
          ) : error ? (
            <div className="text-rose-400 bg-rose-950/20 border border-rose-800/40 p-6 rounded-lg flex items-center gap-4">
              <AlertTriangle size={24} />
              <div>
                <h3 className="font-bold text-rose-300">Error</h3>
                <p className="text-sm text-rose-400/80">{error}</p>
              </div>
            </div>
          ) : orphans.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-600 gap-6 select-none">
              <Sparkles size={64} className="opacity-30" />
              <p className="text-lg font-light text-slate-500">System Clean</p>
              <p className="text-xs uppercase tracking-widest text-slate-600">No orphan memories detected</p>
            </div>
          ) : (
            <div className="max-w-5xl mx-auto space-y-8">
              {/* Deprecated Section */}
              {deprecated.length > 0 && (
                <section>
                  {renderSectionHeader(
                    <Archive size={16} className="text-amber-400/80" />,
                    "Deprecated Versions",
                    "text-amber-400/80",
                    deprecated
                  )}
                  <div className="space-y-2">
                    {deprecated.map(renderCard)}
                  </div>
                </section>
              )}

              {/* Orphaned Section */}
              {orphaned.length > 0 && (
                <section>
                  {renderSectionHeader(
                    <Unlink size={16} className="text-rose-400/80" />,
                    "Orphaned Memories",
                    "text-rose-400/80",
                    orphaned
                  )}
                  <div className="space-y-2">
                    {orphaned.map(renderCard)}
                  </div>
                </section>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
