import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronDown } from 'lucide-react';
import { useLang } from '../i18n/LanguageContext';

const Domains = () => {
  const [openIndex, setOpenIndex] = useState(null);
  const { t } = useLang();

  return (
    <section className="py-24 relative">
      <div className="container mx-auto px-6 max-w-5xl">
        <h2 className="text-3xl md:text-5xl font-bold mb-4 text-center">{t.domains.title1}<span className="text-gradient">{t.domains.titleHighlight}</span></h2>
        <p className="text-slate-400 text-center mb-16">{t.domains.subtitle}</p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {t.domains.list.map((dom, i) => (
            <motion.div 
              initial={{ opacity: 0, y: 10 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              key={i} 
              className="glass rounded-xl overflow-hidden cursor-pointer"
              onClick={() => setOpenIndex(openIndex === i ? null : i)}
            >
              <div className="p-6 flex justify-between items-center bg-white/5 hover:bg-white/10 transition-colors">
                <div>
                  <h3 className="font-bold text-lg text-slate-100">{dom.name}</h3>
                  <p className="text-sm text-teal-400">{dom.count} {t.domains.skillsUnit}</p>
                </div>
                <motion.div animate={{ rotate: openIndex === i ? 180 : 0 }}>
                  <ChevronDown className="text-slate-400" />
                </motion.div>
              </div>
              <AnimatePresence>
                {openIndex === i && (
                  <motion.div 
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    className="px-6 pb-6 text-sm text-slate-400"
                  >
                    <div className="h-px bg-white/10 mb-4" />
                    <strong>{t.domains.keyCapabilities}</strong> {dom.highlights}
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default Domains;
