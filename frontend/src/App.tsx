import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom';
import { PipelineProvider } from './context/PipelineContext';
import { AppLayout } from './components/layout/AppLayout';
import { RouteGuard } from './components/RouteGuard';
import UploadFiles from './pages/UploadFiles';
import ExtractTypes from './pages/ExtractTypes';
import GenerateDataModel from './pages/GenerateDataModel';
import ValidateDataModel from './pages/ValidateDataModel';
import GenerateMutators from './pages/GenerateMutators';
import TestMutators from './pages/TestMutators';
import BuildPackage from './pages/BuildPackage';

const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <PipelineProvider>
        <AppLayout />
      </PipelineProvider>
    ),
    children: [
      // Redirect root to first step
      { index: true, element: <Navigate to="/upload" replace /> },
      // All pipeline steps share the RouteGuard
      {
        element: <RouteGuard />,
        children: [
          { path: 'upload', element: <UploadFiles /> },
          { path: 'extract', element: <ExtractTypes /> },
          { path: 'datamodel', element: <GenerateDataModel /> },
          { path: 'validate', element: <ValidateDataModel /> },
          { path: 'mutators', element: <GenerateMutators /> },
          { path: 'test', element: <TestMutators /> },
          { path: 'package', element: <BuildPackage /> },
        ],
      },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
