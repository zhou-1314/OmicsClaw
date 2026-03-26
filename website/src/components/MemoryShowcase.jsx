import React from 'react';
import { motion } from 'framer-motion';
import { Database, TrendingUp, Settings, Lightbulb } from 'lucide-react';
import { useLang } from '../i18n/LanguageContext';

const iconList = [<Database />, <TrendingUp />, <Settings />, <Lightbulb />];

const MemoryShowcase = () => {
  const { t } = useLang();

  return (
    <section className="py-24 relative overflow-hidden" id="memory">
      <div className="container mx-auto px-6 relative z-10">
        
        <div className="max-w-4xl mx-auto text-center mb-16">
          <h2 className="text-3xl md:text-5xl font-bold mb-4">{t.memory.title1}<span className="text-gradient">{t.memory.titleHighlight}</span></h2>
          <p className="text-slate-400 text-lg">
            {t.memory.subtitle}
          </p>
        </div>

        <motion.div 
          initial={{ opacity: 0, y: 50, rotateX: 10 }}
          whileInView={{ opacity: 1, y: 0, rotateX: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.8 }}
          className="relative max-w-5xl mx-auto"
          style={{ perspective: "1000px" }}
        >
          {/* Main Dashboard Image */}
          <div className="rounded-2xl border border-slate-700/50 overflow-hidden shadow-2xl shadow-teal-500/10 bg-slate-900 aspect-video flex items-center justify-center p-8">
             <img 
                src="https://raw.githubusercontent.com/TianGzlab/OmicsClaw/main/docs/images/memory_system.png" 
                alt="Memory System Explorer"
                className="w-full rounded-lg object-contain mt-2"
             />
          </div>

          {/* Hovering Stat Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-8">
            {t.memory.stats.map((stat, i) => (
              <motion.div 
                key={i}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: 0.2 + (i * 0.1) }}
                className="glass p-4 rounded-xl flex flex-col items-center justify-center text-center group"
              >
                <div className="text-teal-400 mb-2 group-hover:scale-110 transition-transform">
                  {iconList[i]}
                </div>
                <div className="text-sm font-semibold text-slate-200">{stat.label}</div>
                <div className="text-xs text-slate-400">{stat.count}</div>
              </motion.div>
            ))}
          </div>

        </motion.div>
      </div>
    </section>
  );
};

export default MemoryShowcase;
