import { createContext, useCallback, useContext, useState } from "react";

const LocationContext = createContext(null);

export function LocationProvider({ children }) {
  const [location, setLocation] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem("build-mart-location")) || null;
    } catch {
      return null;
    }
  });

  const updateLocation = useCallback((nextLocation) => {
    setLocation(nextLocation);
    localStorage.setItem("build-mart-location", JSON.stringify(nextLocation));
  }, []);

  return (
    <LocationContext.Provider value={{ location, updateLocation }}>
      {children}
    </LocationContext.Provider>
  );
}

export function useLocation() {
  return useContext(LocationContext);
}
