/**
 * Inline SVG icon set for the desktop tree.
 *
 * Hand-rolled rather than `lucide-react-native` so the desktop tree stays
 * pure DOM (no react-native-svg web shim inside a plain-CSS layout) and
 * every glyph inherits `currentColor` from its token-coloured parent.
 * All are decorative — the label next to them carries the meaning.
 */

import type { ReactNode } from 'react';

function Svg({ children, size = 18 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable="false"
      style={{ flex: 'none' }}
    >
      {children}
    </svg>
  );
}

export const IconDashboard = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <rect x="3" y="3" width="7" height="9" rx="1.5" />
    <rect x="14" y="3" width="7" height="5" rx="1.5" />
    <rect x="14" y="12" width="7" height="9" rx="1.5" />
    <rect x="3" y="16" width="7" height="5" rx="1.5" />
  </Svg>
);

export const IconPicks = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M4 17l5-6 4 4 7-8" />
    <path d="M15 7h5v5" />
  </Svg>
);

export const IconPositions = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M3 3v18h18" />
    <rect x="7" y="11" width="3" height="7" rx="1" />
    <rect x="13" y="6" width="3" height="12" rx="1" />
  </Svg>
);

export const IconStrategies = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" />
  </Svg>
);

export const IconReview = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <rect x="5" y="3" width="14" height="18" rx="2" />
    <path d="M9 8h6M9 12h6M9 16h3" />
  </Svg>
);

export const IconInsights = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 3a6 6 0 00-3.5 10.9V17h7v-3.1A6 6 0 0012 3z" />
    <path d="M10 21h4" />
  </Svg>
);

export const IconSettings = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-2.7 1.1v.3a2 2 0 11-4 0v-.2a1.6 1.6 0 00-2.8-1.1l-.1.1a2 2 0 11-2.8-2.8l.1-.1A1.6 1.6 0 003.6 15H3.4a2 2 0 110-4h.2a1.6 1.6 0 001.1-2.8l-.1-.1a2 2 0 112.8-2.8l.1.1A1.6 1.6 0 0010.3 4.4V4.2a2 2 0 114 0v.2a1.6 1.6 0 002.7 1.1l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 001.1 2.7h.2a2 2 0 110 4h-.2a1.6 1.6 0 00-1.5 1z" />
  </Svg>
);

export const IconSun = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </Svg>
);

export const IconMoon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
  </Svg>
);

export const IconBack = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M19 12H5M11 18l-6-6 6-6" />
  </Svg>
);

export const IconRefresh = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M21 12a9 9 0 11-2.6-6.4" />
    <path d="M21 4v5h-5" />
  </Svg>
);

export const IconLogout = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" />
    <path d="M16 17l5-5-5-5M21 12H9" />
  </Svg>
);

export const IconCheck = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M20 6L9 17l-5-5" />
  </Svg>
);

export const IconCross = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Svg>
);

export const IconShield = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z" />
  </Svg>
);

export const IconSpark = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />
  </Svg>
);
