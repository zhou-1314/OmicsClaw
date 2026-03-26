import React from 'react';
import { Dna, FileText, Bot } from 'lucide-react';
import GithubIcon from './GithubIcon';
import { useLang } from '../i18n/LanguageContext';

const Footer = () => {
  const { t } = useLang();

  return (
    <footer className="border-t border-white/5 bg-slate-950 pt-16 pb-8 relative z-10">
      <div className="container mx-auto px-6">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-12 mb-16">
          <div className="md:col-span-2">
            <div className="flex items-center gap-2 mb-4">
              <Dna className="text-teal-400 w-8 h-8" />
              <span className="text-2xl font-bold tracking-tight text-white">Omics<span className="text-teal-400">Claw</span></span>
            </div>
            <p className="text-slate-400 max-w-md">
              {t.footer.desc}
            </p>
          </div>
          
          <div>
            <h4 className="text-white font-semibold mb-4">{t.footer.resources}</h4>
            <ul className="space-y-3">
              <li><a href="https://github.com/TianGzlab/OmicsClaw" className="text-slate-400 hover:text-teal-400 transition-colors flex items-center gap-2"><GithubIcon className="w-4 h-4"/> {t.footer.ghRepo}</a></li>
              <li><a href="https://github.com/TianGzlab/OmicsClaw/blob/main/docs/INSTALLATION.md" className="text-slate-400 hover:text-teal-400 transition-colors flex items-center gap-2"><FileText className="w-4 h-4"/> {t.footer.install}</a></li>
              <li><a href="https://github.com/TianGzlab/OmicsClaw/blob/main/AGENTS.md" className="text-slate-400 hover:text-teal-400 transition-colors flex items-center gap-2"><Bot className="w-4 h-4"/> {t.footer.agents}</a></li>
            </ul>
          </div>
          
          <div>
            <h4 className="text-white font-semibold mb-4">{t.footer.legal}</h4>
            <ul className="space-y-3">
              <li><a href="https://github.com/TianGzlab/OmicsClaw/blob/main/LICENSE" className="text-slate-400 hover:text-teal-400 transition-colors">{t.footer.license}</a></li>
              <li><span className="text-slate-500">{t.footer.research}</span></li>
            </ul>
          </div>
        </div>
        
        <div className="border-t border-white/5 pt-8 text-center text-slate-500 text-sm">
          <p>{t.footer.copyright}</p>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
