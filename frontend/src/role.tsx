import { createContext, useContext } from "react";
import type { Role } from "./types";

/** The role of the signed-in user. The API checks each request; the website only hides what the role cannot do. */
export const RoleContext = createContext<Role>("viewer");
export const UserNameContext = createContext<string>("");

export function useCan() {
  const role = useContext(RoleContext);
  return { role, work: role !== "viewer", admin: role === "admin" };
}

export const ROLE_LABELS: Record<Role, string> = { viewer: "Viewer", engineer: "Engineer", admin: "Admin" };
