export default function Alert({ children, tone = 'error' }) {
  if (!children) return null

  const tones = {
    error: 'bg-red-50 text-red-700',
    info: 'bg-brand-50 text-brand-700',
    warning: 'bg-amber-50 text-amber-800',
  }

  return (
    <p role="alert" className={`rounded-lg px-3 py-2 text-sm ${tones[tone] ?? tones.error}`}>
      {children}
    </p>
  )
}
