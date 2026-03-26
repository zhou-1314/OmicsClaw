import React from 'react';
import { motion } from 'framer-motion';
import { ClipboardList, Search, Code2, BarChart3, PenTool, ShieldCheck, ArrowDown } from 'lucide-react';
import { useLang } from '../i18n/LanguageContext';

const Architecture = () => {
  const { t } = useLang();

  const agents = [
    { icon: <ClipboardList className="w-6 h-6" />, color: "text-teal-400", bg: "bg-teal-500/10 border-teal-500/30" },
    { icon: <Search className="w-6 h-6" />, color: "text-cyan-400", bg: "bg-cyan-500/10 border-cyan-500/30" },
    { icon: <Code2 className="w-6 h-6" />, color: "text-blue-400", bg: "bg-blue-500/10 border-blue-500/30" },
    { icon: <BarChart3 className="w-6 h-6" />, color: "text-violet-400", bg: "bg-violet-500/10 border-violet-500/30" },
    { icon: <PenTool className="w-6 h-6" />, color: "text-amber-400", bg: "bg-amber-500/10 border-amber-500/30" },
    { icon: <ShieldCheck className="w-6 h-6" />, color: "text-rose-400", bg: "bg-rose-500/10 border-rose-500/30" },
  ];

  return (
    <section className="py-24 relative" id="architecture">
      <div className="container mx-auto px-6">
        <div className="text-center mb-16">
          <h2 className="text-3xl md:text-5xl font-bold mb-4">{t.arch.title1}<span className="text-gradient">{t.arch.titleHighlight}</span></h2>
          <p className="text-slate-400 max-w-3xl mx-auto text-lg">
            {t.arch.subtitle}
          </p>
        </div>

        {/* Agent pipeline flow */}
        <div className="max-w-3xl mx-auto">
          {t.arch.agents.map((agent, index) => (
            <React.Fragment key={index}>
              <motion.div
                initial={{ opacity: 0, x: index % 2 === 0 ? -30 : 30 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true }}
                transition={{ delay: index * 0.1 }}
                className={`flex items-start gap-4 p-5 rounded-xl border ${agents[index].bg} hover:scale-[1.02] transition-transform`}
              >
                <div className={`shrink-0 w-12 h-12 rounded-lg flex items-center justify-center ${agents[index].bg} ${agents[index].color}`}>
                  {agents[index].icon}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`text-xs font-mono px-2 py-0.5 rounded-full ${agents[index].bg} ${agents[index].color}`}>{agent.tag}</span>
                    <h3 className="font-bold text-white">{agent.title}</h3>
                  </div>
                  <p className="text-slate-400 text-sm leading-relaxed">{agent.desc}</p>
                  {agent.tools && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {agent.tools.map((tool, i) => (
                        <span key={i} className="text-[10px] font-mono bg-white/5 border border-white/10 px-2 py-0.5 rounded text-slate-400">{tool}</span>
                      ))}
                    </div>
                  )}
                </div>
              </motion.div>

              {/* Connector arrow */}
              {index < t.arch.agents.length - 1 && (
                <div className="flex justify-center py-2 text-slate-700">
                  <ArrowDown className="w-5 h-5" />
                </div>
              )}
            </React.Fragment>
          ))}

          {/* Feedback loop indicator */}
          <motion.div
            initial={{ opacity: 0 }}
            whileInView={{ opacity: 1 }}
            viewport={{ once: true }}
            className="mt-6 text-center"
          >
            <div className="inline-flex items-center gap-2 px-4 py-2 rounded-full glass text-xs text-slate-400">
              <span className="w-2 h-2 rounded-full bg-rose-400 animate-pulse" />
              {t.arch.loopLabel}
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default Architecture;
