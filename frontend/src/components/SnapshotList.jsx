import React from 'react';
import clsx from 'clsx';

const getActionColor = (action) => {
  if (action === 'created') return 'emerald';
  if (action === 'deleted') return 'rose';
  return 'amber'; // modified
};

const getActionLabel = (table, action) => {
  let entityName = table;
  if (table === 'memories') entityName = 'Memory';
  else if (table.endsWith('s')) entityName = table.slice(0, -1);
  
  const capitalizedEntity = entityName.charAt(0).toUpperCase() + entityName.slice(1);
  const capitalizedAction = action ? action.charAt(0).toUpperCase() + action.slice(1) : 'Modified';
  return `${capitalizedEntity} ${capitalizedAction}`;
};

const COLOR_CLASSES = {
  emerald: {
    active: "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]",
    idle:   "bg-emerald-900",
    label:  "text-emerald-700",
  },
  rose: {
    active: "bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)]",
    idle:   "bg-rose-900",
    label:  "text-rose-700",
  },
  amber: {
    active: "bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.6)]",
    idle:   "bg-amber-900",
    label:  "text-amber-700",
  },
};

const SnapshotList = ({ snapshots, selectedId, onSelect }) => {
  if (snapshots.length === 0) {
    return (
      <div className="text-center py-10 text-slate-600 text-xs tracking-wide uppercase">
        Empty Sequence
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      {snapshots.map((item) => {
        const isSelected = item.node_uuid === selectedId;
        const colorName = getActionColor(item.action);
        const colors = COLOR_CLASSES[colorName];
        const labelText = getActionLabel(item.top_level_table, item.action);

        return (
          <button
            key={item.node_uuid}
            onClick={() => onSelect(item)}
            className={clsx(
              "group relative text-left py-3 px-5 border-l-2 transition-all duration-200 outline-none w-full hover:bg-white/[0.02]",
              isSelected
                ? "border-indigo-500 bg-white/[0.03]"
                : "border-transparent text-slate-500 hover:text-slate-300"
            )}
          >
            {isSelected && (
              <div className="absolute inset-0 bg-gradient-to-r from-indigo-500/10 to-transparent pointer-events-none" />
            )}

            <div className="flex items-center gap-3 relative z-10">
              <div className={clsx(
                "flex-shrink-0 w-1.5 h-1.5 rounded-full transition-colors",
                isSelected ? colors.active : colors.idle
              )} />

              <div className="min-w-0 flex-1">
                <div className={clsx(
                  "font-medium text-xs truncate transition-colors",
                  isSelected ? "text-slate-200" : "text-slate-400 group-hover:text-slate-300"
                )}>
                  {item.display_uri}
                </div>
                <div className="mt-0.5 flex justify-between items-center pr-2">
                  <span className={clsx(
                    "text-[10px] uppercase tracking-wider font-bold",
                    colors.label
                  )}>
                    {labelText}
                  </span>
                  {item.row_count > 1 && (
                    <span className="text-[9px] text-slate-600">
                      {item.row_count} rows
                    </span>
                  )}
                </div>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
};

export default SnapshotList;
