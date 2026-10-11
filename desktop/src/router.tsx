import { createHashRouter, Navigate } from "react-router-dom";
import { AppLayout } from "./app/AppLayout";
import { ChatScreen } from "./screens/ChatScreen";
import { FoldersScreen } from "./screens/FoldersScreen";
import { StatusScreen } from "./screens/StatusScreen";
import { SettingsScreen } from "./screens/SettingsScreen";

export const router = createHashRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/chat" replace /> },
      { path: "chat", element: <ChatScreen /> },
      { path: "folders", element: <FoldersScreen /> },
      { path: "status", element: <StatusScreen /> },
      { path: "settings", element: <SettingsScreen /> },
    ],
  },
]);
