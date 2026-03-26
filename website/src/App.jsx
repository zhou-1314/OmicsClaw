import React from 'react';
import { LanguageProvider } from './i18n/LanguageContext';
import Navbar from './components/Navbar';
import Hero from './components/Hero';
import UnifiedControl from './components/UnifiedControl';
import Architecture from './components/Architecture';
import CorePillars from './components/CorePillars';
import MemoryShowcase from './components/MemoryShowcase';
import Domains from './components/Domains';
import Team from './components/Team';
import Footer from './components/Footer';

function App() {
  return (
    <LanguageProvider>
      <div className="relative w-full min-h-screen">
        {/* Background glow effects */}
        <div className="fixed top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-teal-900/20 blur-[120px] pointer-events-none" />
        <div className="fixed bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-cyan-900/20 blur-[120px] pointer-events-none" />
        
        <Navbar />
        <main className="relative z-10">
          <Hero />
          <UnifiedControl />
          <Architecture />
          <CorePillars />
          <MemoryShowcase />
          <Domains />
          <Team />
        </main>
        <Footer />
      </div>
    </LanguageProvider>
  );
}

export default App;
