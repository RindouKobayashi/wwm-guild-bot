# Included visual components

React Bits by David Haz: https://github.com/DavidHDev/react-bits

Downloaded November 1, 2026 from `src/ts-default/`:

- Components/SpotlightCard/SpotlightCard.tsx and CSS
- Components/MagicBento/MagicBento.tsx and CSS
- Animations/AnimatedContent/AnimatedContent.tsx

The complete license is included in `REACT-BITS-LICENSE.md` (MIT + Commons Clause License Condition v1.0). This panel incorporates these components into an application. It does not distribute them as a component product.

WWM modifications: removed `use client` directives for Vite; removed MagicBento's demo artwork and third-party icon dependencies, retaining its layout and lighting behavior. Particle, tilt, magnetism and click effects are disabled by the caller. AnimatedContent and SpotlightCard retain upstream reduced-motion support.

Other dependencies and their license metadata are available in the locked npm package distribution: React/React DOM, GSAP, TanStack Virtual, TypeScript and Vite.

The dedicated terminal bundles xterm.js 5.5.0 and addon-fit 0.10.0. Their MIT notices are retained in XTERM-LICENSE.txt and XTERM-FIT-LICENSE.txt. AsyncSSH and pywinpty are Python runtime dependencies distributed through requirements-panel.txt; their upstream license metadata accompanies their package distributions.
