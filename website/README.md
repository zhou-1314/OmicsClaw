# OmicsClaw Official Website

> Dark-themed, animated landing page for the [OmicsClaw](https://github.com/TianGzlab/OmicsClaw) multi-omics analysis platform.

## Tech Stack

| Category       | Tool                                                                 |
| -------------- | -------------------------------------------------------------------- |
| Framework      | [React 19](https://react.dev/) + [Vite 8](https://vite.dev/)        |
| Styling        | [Tailwind CSS 3](https://tailwindcss.com/)                           |
| Animation      | [Framer Motion 12](https://www.framer.com/motion/)                   |
| Icons          | [Lucide React](https://lucide.dev/) + custom GitHub SVG              |
| Utilities      | clsx, tailwind-merge, react-countup                                  |
| Linting        | ESLint 9                                                             |

## Quick Start

```bash
# Install dependencies
npm install

# Start dev server (localhost only)
npm run dev

# Start dev server (accessible from LAN / SSH tunnel)
npm run dev:host

# Build for production
npm run build

# Preview production build
npm run preview
```

## Remote Access

If the dev server runs on a remote machine, use SSH port forwarding to access it locally:

```bash
ssh -L 5173:localhost:5173 user@remote-server
# Then open http://localhost:5173 in your local browser
```

## Project Structure

```
website/
├── index.html                    # Entry HTML
├── package.json                  # Dependencies & scripts
├── vite.config.js                # Vite + React plugin
├── tailwind.config.js            # Tailwind CSS v3 config
├── postcss.config.js             # PostCSS (autoprefixer + tailwindcss)
├── public/
│   ├── favicon.svg               # Site favicon
│   └── icons.svg                 # SVG sprite
└── src/
    ├── main.jsx                  # React DOM entry
    ├── App.jsx                   # Root component (LanguageProvider wrapper)
    ├── index.css                 # Global styles + Tailwind directives
    ├── i18n/
    │   ├── LanguageContext.jsx    # React context for EN/ZH switching
    │   └── translations.js       # All UI strings (English + Chinese)
    └── components/
        ├── Navbar.jsx            # Glassmorphism nav bar + language toggle
        ├── Hero.jsx              # Hero section with CTAs
        ├── UnifiedControl.jsx    # CLI/TUI + Messaging bots showcase
        ├── Architecture.jsx      # 6-agent pipeline vertical flow
        ├── CorePillars.jsx       # 3 pillars: Pipeline / Chat / Memory
        ├── MemoryShowcase.jsx    # Memory Explorer dashboard preview
        ├── Domains.jsx           # 6 omics domains accordion
        ├── Team.jsx              # Team member cards
        ├── Footer.jsx            # Footer with resources & legal
        └── GithubIcon.jsx        # Custom GitHub SVG (brand icon)
```

## Features

- **中英双语切换** — Click the 🌐 button in the navbar to toggle between English and Chinese
- **Dark Glassmorphism** — Deep slate-950 background with teal/cyan accent gradients
- **Multi-Agent Architecture** — Vertical pipeline showcasing all 6 agents (Planner → Research → Coding → Analysis → Writing → Reviewer) with tool tags
- **Responsive Design** — Mobile-first layout with adaptive grids
- **Smooth Animations** — Scroll-triggered entrance animations via Framer Motion
- **SVG Schematics** — Pure CSS/SVG diagrams for graph memory and CLI mockups (no external images required)

## Design System

| Token               | Value                             |
| -------------------- | --------------------------------- |
| Background           | `slate-950` (#020617)             |
| Text primary         | `slate-200` (#e2e8f0)             |
| Accent gradient      | `teal-400` → `cyan-400`          |
| Glass background     | `white/5` + `backdrop-blur-md`    |
| Card background      | `slate-900/50` + `backdrop-blur`  |
| Font                 | Inter (300–700)                   |

## Deployment

```bash
# Build static files
npm run build

# Output in dist/ — deploy to any static host:
# GitHub Pages, Vercel, Netlify, Cloudflare Pages, etc.
```

## Relationship to Main Project

This website is a **standalone static site** and does **not** interfere with the existing `frontend/` directory (Memory Explorer Dashboard). Both projects share the same React + Vite + Tailwind stack but are completely independent.

## License

Same as the main OmicsClaw project — [Apache-2.0 License](https://github.com/TianGzlab/OmicsClaw/blob/main/LICENSE).
