/**
 * Line icons, drawn here rather than installed.
 *
 * A landing page is not worth a dependency, and an icon package would ship
 * hundreds of glyphs to render seven. These are one consistent system — 24×24,
 * stroked in `currentColor` at 1.5 — so they take their color from whatever
 * they sit in and stay legible on the dark section as well as the light ones.
 *
 * `aria-hidden` on every one: each icon sits beside a heading that already says
 * the same thing, so announcing it twice is noise to a screen reader.
 */
function Glyph({ children, className = 'h-6 w-6' }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

export function ShieldCheck(props) {
  return (
    <Glyph {...props}>
      <path d="M12 3 4.5 6v5.5c0 4.5 3.2 7.3 7.5 8.5 4.3-1.2 7.5-4 7.5-8.5V6L12 3Z" />
      <path d="m9 12 2 2 4-4" />
    </Glyph>
  )
}

export function IdCard(props) {
  return (
    <Glyph {...props}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <circle cx="8.5" cy="11" r="2" />
      <path d="M5.5 16.5c.6-1.5 1.7-2.2 3-2.2s2.4.7 3 2.2" />
      <path d="M14 10h4M14 13.5h4" />
    </Glyph>
  )
}

export function Key(props) {
  return (
    <Glyph {...props}>
      <rect x="4.5" y="10" width="15" height="10" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
      <path d="M12 14v2.5" />
    </Glyph>
  )
}

export function TwoNotes(props) {
  return (
    <Glyph {...props}>
      <rect x="2.5" y="4" width="11" height="8" rx="1.5" />
      <rect x="10.5" y="12" width="11" height="8" rx="1.5" />
    </Glyph>
  )
}

export function CalendarPlus(props) {
  return (
    <Glyph {...props}>
      <rect x="3.5" y="5" width="17" height="15" rx="2" />
      <path d="M8 3v4M16 3v4M3.5 10h17" />
      <path d="M12 13v4M10 15h4" />
    </Glyph>
  )
}

export function PriceTag(props) {
  return (
    <Glyph {...props}>
      <path d="M3.5 11.5V4.5a1 1 0 0 1 1-1h7l8.5 8.5a1.4 1.4 0 0 1 0 2l-6 6a1.4 1.4 0 0 1-2 0L3.5 11.5Z" />
      <circle cx="7.75" cy="7.75" r="1.25" />
    </Glyph>
  )
}

export function CardCheck(props) {
  return (
    <Glyph {...props}>
      <rect x="2.5" y="5" width="19" height="14" rx="2" />
      <path d="M2.5 9.5h19" />
      <path d="m9 15 2 2 4-4" />
    </Glyph>
  )
}

export function Chat(props) {
  return (
    <Glyph {...props}>
      <path d="M3.5 6.5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9l-4 3.5V15.5H5.5a2 2 0 0 1-2-2Z" />
      <path d="M8 9h8M8 12h5" />
    </Glyph>
  )
}

export function Route(props) {
  return (
    <Glyph {...props}>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="18" cy="18" r="2.5" />
      <path d="M6 8.5v3a4 4 0 0 0 4 4h5.5" />
      <path d="M13.5 13 16 15.5 13.5 18" />
    </Glyph>
  )
}

export function Clock(props) {
  return (
    <Glyph {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </Glyph>
  )
}
