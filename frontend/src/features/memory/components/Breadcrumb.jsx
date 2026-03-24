import React from 'react';
import { ChevronRight, Home } from 'lucide-react';
import clsx from 'clsx';

const Breadcrumb = ({ items, onNavigate }) => (
  <div className="flex items-center gap-2 overflow-x-auto no-scrollbar mask-linear-fade py-2">
    <button 
      onClick={() => onNavigate('')}
      className="p-1.5 rounded-lg hover:bg-white/10 text-slate-400 hover:text-indigo-300 transition-colors bg-black/20 border border-white/5 shadow-inner"
    >
      <Home size={14} />
    </button>
    
    {items.map((crumb, i) => (
      <React.Fragment key={crumb.path}>
        <ChevronRight size={14} className="text-slate-600 flex-shrink-0" />
        <button
          onClick={() => onNavigate(crumb.path)}
          className={clsx(
            "px-3 py-1.5 rounded-full text-[13px] font-medium transition-all whitespace-nowrap border",
            i === items.length - 1
              ? "bg-indigo-500/20 text-indigo-200 border-indigo-400/30 shadow-[0_0_15px_rgba(99,102,241,0.15)]"
              : "border-transparent text-slate-400 hover:text-slate-200 hover:bg-white/5"
          )}
        >
          {crumb.name || crumb.label}
        </button>
      </React.Fragment>
    ))}
  </div>
);

export default Breadcrumb;
