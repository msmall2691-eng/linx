/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#101828',
        // **A full ramp, not five stops.** The landing page kept reaching for
        // shades that were not here — `text-brand-300` was written once and
        // silently resolved to nothing, because a missing Tailwind colour is
        // not an error, it is no class at all. Filling the ramp is what stops
        // the next tint being invented in a hex literal on one component.
        brand: {
          50: '#eef6ff',
          100: '#d9ecff',
          200: '#bcddff',
          300: '#8ec6fb',
          400: '#5aa7f2',
          500: '#2a7de1',
          600: '#1f66bd',
          700: '#1a5399',
          800: '#18447b',
          900: '#173a66',
        },
        // A second hue, so the page is not one blue at nine opacities. Used
        // for the things that are *finished* — a job done, a check passed —
        // which is why it is a green rather than a second cool tone.
        accent: {
          50: '#effbf6',
          100: '#d6f5e8',
          200: '#aeead3',
          400: '#4fc79c',
          500: '#22ab7d',
          600: '#158a64',
          700: '#136c51',
        },
        // Warm, for the parts that are about people rather than process. It is
        // deliberately not in the urgency ramp: nothing here may look like a
        // rung, because the badges are the one place colour carries meaning.
        sun: {
          50: '#fff7ed',
          100: '#ffedd5',
          400: '#fb923c',
          500: '#f2760c',
          600: '#d45e06',
        },
        // The urgency ladder is the product's core pricing signal, so it gets
        // real, consistent colors rather than ad-hoc ones per screen.
        urgency: {
          standard: '#667085',
          soon: '#b54708',
          urgent: '#d92d20',
          sameday: '#7a271a',
        },
      },
    },
  },
  plugins: [],
}
