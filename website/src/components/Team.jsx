import React from 'react';
import { motion } from 'framer-motion';
import { Mail } from 'lucide-react';
import GithubIcon from './GithubIcon';
import { useLang } from '../i18n/LanguageContext';

const teamMembers = [
  { name: "Luyi Tian", role: "Principal Investigator", mail: "mailto:tian_luyi@gzlab.ac.cn", gh: null },
  { name: "Weige Zhou", role: "Lead Developer", mail: null, gh: "https://github.com/zhou-1314" },
  { name: "Liying Chen", role: "Developer", mail: null, gh: "https://github.com/chenly255" },
  { name: "Pengfei Yin", role: "Developer", mail: null, gh: "https://github.com/astudentfromsustech" },
];

const Team = () => {
  const { t } = useLang();

  return (
    <section className="py-24 relative" id="team">
      <div className="container mx-auto px-6">
        <div className="text-center mb-16">
          <h2 className="text-3xl md:text-5xl font-bold mb-4">{t.team.title1}<span className="text-gradient">{t.team.titleHighlight}</span></h2>
          <p className="text-slate-400 max-w-2xl mx-auto text-lg">
            {t.team.subtitle}
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 max-w-6xl mx-auto">
          {teamMembers.map((member, i) => (
            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.1 }}
              key={i}
              className="glass p-8 text-center group hover:bg-slate-800/50 transition-colors"
            >
              <div className="w-24 h-24 mx-auto bg-gradient-to-br from-teal-500 to-cyan-500 rounded-full mb-6 p-1 group-hover:scale-105 transition-transform">
                <div className="w-full h-full bg-slate-900 rounded-full flex items-center justify-center text-3xl font-bold text-slate-200">
                  {member.name.split(' ').map(n => n[0]).join('')}
                </div>
              </div>
              <h3 className="text-xl font-bold mb-1">{member.name}</h3>
              <p className="text-teal-400 text-sm mb-6">{member.role}</p>
              
              <div className="flex justify-center gap-3">
                {member.gh && (
                  <a href={member.gh} target="_blank" rel="noreferrer" className="w-10 h-10 rounded-full glass hover:bg-white/10 flex items-center justify-center transition-colors">
                    <GithubIcon className="w-5 h-5 text-slate-300" />
                  </a>
                )}
                {member.mail && (
                  <a href={member.mail} className="w-10 h-10 rounded-full glass hover:bg-white/10 flex items-center justify-center transition-colors">
                    <Mail className="w-5 h-5 text-slate-300" />
                  </a>
                )}
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default Team;
