/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        ink: { 900: '#080b14', 800: '#0d1220', 700: '#141b2d', 600: '#1c2438', 500: '#2a3450' },
        up: '#ef4d56',
        down: '#22c55e',
        accent: '#5b8cff',
      },
      fontFamily: {
        mono: ['SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
