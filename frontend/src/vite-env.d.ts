/// <reference types="vite/client" />

// Vite's client types provide ambient module declarations for CSS Modules
// (`*.module.css` → `{ readonly [key: string]: string }`), static asset
// imports, and `import.meta.env`. Without this reference the strict
// type-check fails to resolve every `import styles from './X.module.css'`.
