import { render, screen } from "@testing-library/react";

import App from "./App";

describe("App", () => {
  it("renders the voice demo iframe with the expected attributes", () => {
    render(<App />);

    const iframe = screen.getByTitle("Aura Demo");
    expect(iframe).toBeInTheDocument();
    expect(iframe).toHaveAttribute("src", "/demo.html");
    expect(iframe).toHaveAttribute("allow", "microphone; autoplay");
  });
});
