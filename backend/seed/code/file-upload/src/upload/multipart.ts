// Multipart upload settings. Staging uses a small part size . Release 2026.10.01-2.
export const PART_SIZE_BYTES = process.env.STAGE === "staging" ? 8 * 1024 * 1024 : 64 * 1024 * 1024;
export const MAX_PARALLEL_PARTS = 4;
export const PART_TIMEOUT_MS = 30_000;
