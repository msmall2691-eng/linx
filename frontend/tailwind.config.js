/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#101828',
        brand: {
          50: '#eef6ff',
          100: '#d9ecff',
          500: '#2a7de1',
          600: '#1f66bd',
          700: '#1a5399',
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
