export default function Loading() {
  return (
    <div className="card">
      <div className="skeleton" style={{ width: "40%" }} />
      <div className="skeleton" />
      <div className="skeleton" />
      <div className="skeleton" style={{ width: "70%" }} />
    </div>
  );
}
