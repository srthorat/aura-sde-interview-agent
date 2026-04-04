export default function App() {
  return (
    <div style={{ height: "100vh", width: "100%" }}>
      <iframe
        allow="microphone; autoplay"
        src="/demo.html"
        style={{ border: 0, display: "block", height: "100%", width: "100%" }}
        title="Aura Demo"
      />
    </div>
  );
}