import React from 'react';
import { ChevronRight, Folder, FileText, AlertTriangle, Link2 } from 'lucide-react';
import clsx from 'clsx';
import PriorityBadge from './PriorityBadge';

const NodeGridCard = ({ node, currentDomain, onClick }) => {
  const isCrossDomain = node.domain && node.domain !== currentDomain;
  return (
  <button 
    onClick={onClick}
    className={clsx(
      "glass-card group relative flex flex-col items-start p-5 rounded-2xl text-left w-full h-full overflow-hidden",
      isCrossDomain
        ? "border-violet-500/10 hover:border-violet-500/40 hover:shadow-[0_8px_30px_rgba(139,92,246,0.15)]"
        : "border-indigo-500/10 hover:border-indigo-500/40 shadow-[0_4px_20px_rgba(0,0,0,0.1)] hover:shadow-[0_8px_30px_rgba(99,102,241,0.15)]"
    )}
  >
    <div className="absolute inset-0 bg-gradient-to-br from-indigo-500/5 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
    
    <div className="flex items-center gap-3 mb-4 w-full">
      <div className="p-2.5 rounded-xl bg-black/30 border border-white/5 group-hover:bg-white/5 group-hover:border-white/10 text-slate-400 group-hover:text-indigo-300 transition-all flex-shrink-0 shadow-inner">
         {node.approx_children_count > 0 ? <Folder size={18} /> : <FileText size={18} />}
      </div>
      <div className="min-w-0 flex-1">
        <h3 className="text-[15px] font-semibold text-slate-200 group-hover:text-white transition-colors break-words line-clamp-1 tracking-tight">
          {node.name || node.path.split('/').pop()}
        </h3>
        {isCrossDomain && (
          <span className="inline-flex items-center gap-1 mt-1 px-1.5 py-0.5 text-[10px] font-mono text-violet-400/80 bg-violet-950/40 border border-violet-800/30 rounded">
            <Link2 size={9} />
            {node.domain}://
          </span>
        )}
      </div>
      <PriorityBadge priority={node.priority} />
    </div>
    
    {node.disclosure && (
      <div className="w-full mb-2">
        <p className="text-[11px] text-amber-500/70 leading-snug line-clamp-2 flex items-start gap-1">
          <AlertTriangle size={11} className="flex-shrink-0 mt-0.5" />
          <span className="italic">{node.disclosure}</span>
        </p>
      </div>
    )}
    
    <div className="w-full flex-1">
        {node.content_snippet ? (
            <p className="text-xs text-slate-400 leading-relaxed line-clamp-3 group-hover:text-slate-300 transition-colors">
                {node.content_snippet}
            </p>
        ) : (
            <p className="text-xs text-slate-600 italic">No preview available</p>
        )}
    </div>

    <ChevronRight size={16} className="absolute bottom-5 right-4 text-indigo-400/50 opacity-0 group-hover:opacity-100 group-hover:translate-x-1 transition-all duration-300 drop-shadow-md" />
  </button>
  );
};

export default NodeGridCard;
