import '@testing-library/jest-dom/vitest'

// Mock createRange for environments like jsdom if needed
if (typeof document !== 'undefined' && !document.createRange) {
  document.createRange = () => {
    const range = new Range()
    return range
  }
}
