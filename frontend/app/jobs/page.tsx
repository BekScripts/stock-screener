import { JobConsole } from "@/components/job-console";

/**
 * The control panel.
 *
 * Everything on it is a client component: what it shows changes while a command
 * runs, and a server-rendered snapshot of a job that finished two seconds ago
 * would be wrong by the time it arrived.
 */
export default function JobsPage() {
  return <JobConsole />;
}
