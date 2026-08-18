import Link from "next/link";

export default function NotFound() {
  return (
    <div className="card">
      <h2>No such company</h2>
      <p className="empty">
        That ticker is not stored. Only companies the scan has ingested have a page.
      </p>
      <Link className="tab" href="/">
        Back to rankings
      </Link>
    </div>
  );
}
