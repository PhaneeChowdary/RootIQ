// Vite imports a file as text with "?raw" . Used for the provider logos.
declare module "*?raw" {
  const content: string;
  export default content;
}
